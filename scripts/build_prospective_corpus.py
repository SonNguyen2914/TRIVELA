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
  2. the snapshot was CAPTURED before the fixture's kickoff, witnessed
     by the document's own `captured_at` when present and by the file's
     mtime otherwise. GIT DOES NOT PRESERVE MTIME, so a snapshot
     committed to survive the container returns from a clone with an
     mtime of the checkout — after every past kickoff — and mtime alone
     would reject every row in it. `captured_at` is written at fetch
     time by scripts/capture_pre_kickoff.py; for a committed file the
     git commit time is the independent upper bound on that claim.

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
        doc = json.loads(path.read_text())
        # GIT DOES NOT PRESERVE MTIME. A snapshot committed so it
        # survives the container comes back from a clone with an mtime
        # of the checkout — i.e. now — which is after every past
        # kickoff, so condition 2 would reject every row in it. Verified
        # in this repo: a document committed 2026-08-05 04:22 PDT
        # carried an mtime of 11:22 in a fresh container.
        #
        # So an explicit `captured_at`, written into the document by
        # scripts/capture_pre_kickoff.py at fetch time, WINS over mtime.
        # It is a claim by the writer rather than a filesystem fact —
        # but for a committed file the git commit time is an independent
        # upper bound to check it against, which mtime never was, since
        # mtime can be set to anything by touching the file.
        stated = _utc(doc.get("captured_at"))
        captured = stated or datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc)
        witness = "captured_at" if stated else "file_mtime"
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
                                "why": f"captured {captured.isoformat()} "
                                       f"at or after kickoff "
                                       f"{ko.isoformat()} "
                                       f"(witness: {witness})"})
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
                "captured_at_witness": witness,
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


# the fields that make a row a piece of EVIDENCE rather than a label. If
# two builds disagree on any of these for the same fixture, one of them
# is wrong about a settled fact and the caller has to say which — the
# same reason a journal resolution is immutable and a correction is a
# new row citing the old one.
_EVIDENCE_FIELDS = ("_read", "kickoff_utc", "goals", "final_status")


def _rowkey(r: dict) -> str:
    return str(r.get("fixture_id") or "")


def _conflicts(old: dict, new: dict) -> list:
    return [f for f in _EVIDENCE_FIELDS if old.get(f) != new.get(f)]


def reconcile_with_existing(out: Path, rows: list, *, merge: bool,
                            replace: bool) -> dict:
    """Decide whether writing `rows` over `out` would destroy evidence.

    Three outcomes, and the DEFAULT is the safe one:

      no existing file, or the new rows cover everything it holds
                        -> write, nothing is lost
      rows would vanish, no flag
                        -> REFUSE, naming every fixture that would go
      --merge           -> union by fixture_id, still refusing a conflict
      --replace         -> write anyway, naming what was discarded

    A conflict is never resolved silently under any flag. Two builds
    disagreeing about a settled score or a frozen read is not a retry;
    it means one of them is wrong, and picking a winner by argument
    order is how a corpus quietly acquires a false row.
    """
    new_by = {_rowkey(r): r for r in rows}
    if not out.exists():
        return {"rows": rows, "write_mode": "created"}
    try:
        existing = json.loads(out.read_text()).get("rows") or []
    except (OSError, ValueError) as exc:
        return {"refused": (f"the existing corpus at {out} could not be "
                            f"read, so what writing would destroy is "
                            f"unknown"),
                "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
                "means": "fix or move the file deliberately; do not guess"}
    old_by = {_rowkey(r): r for r in existing}

    conflicts = []
    for k in sorted(set(old_by) & set(new_by)):
        diff = _conflicts(old_by[k], new_by[k])
        if diff:
            conflicts.append({"fixture_id": k, "fields": diff,
                              "home": (old_by[k].get("home") or {}).get("name")})
    if conflicts:
        return {"refused": ("this build disagrees with the existing corpus "
                            "about settled facts. One of them is wrong and "
                            "this script will not pick"),
                "conflicts": conflicts,
                "means": ("re-check the inputs. If the OLD row is wrong, "
                          "move it aside deliberately — do not let an "
                          "argument order decide which evidence survives")}

    lost = sorted(set(old_by) - set(new_by))
    if not lost:
        return {"rows": rows, "write_mode": "rewrote_same_fixtures",
                "reproduced": len(set(old_by) & set(new_by))}

    named = [{"fixture_id": k,
              "fixture": f"{(old_by[k].get('home') or {}).get('name')} v "
                         f"{(old_by[k].get('away') or {}).get('name')}",
              "kickoff_utc": old_by[k].get("kickoff_utc"),
              "source_file": old_by[k].get("source_file")} for k in lost]

    if replace:
        return {"rows": rows, "write_mode": "replaced", "discarded": named,
                "added": sorted(set(new_by) - set(old_by))}
    if merge:
        merged = existing + [r for k, r in new_by.items() if k not in old_by]
        merged.sort(key=lambda r: r.get("kickoff_utc") or "")
        return {"rows": merged, "write_mode": "merged",
                "preserved": named,
                "added": sorted(set(new_by) - set(old_by))}

    return {"refused": (f"writing would DELETE {len(lost)} row(s) the "
                        f"existing corpus holds and this run does not "
                        f"reproduce"),
            "would_be_lost": named,
            "why_this_matters": (
                "these rows may be the only surviving record of their "
                "reads. Pre-kickoff snapshots taken before the durable "
                "capture landed (#84) lived in session scratch and died "
                "with their containers, so re-running cannot recreate "
                "them"),
            "means": ("--merge to keep them and add this run's rows, or "
                      "--replace to discard them deliberately. There is "
                      "no default, because the default would be a guess "
                      "about irreplaceable evidence")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="append", required=True,
                    help="pre-kickoff fixtures payload; repeatable")
    ap.add_argument("--results", default=None,
                    help="settled fixtures payload (default: live API)")
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--out", default=None)
    # WRITING THIS FILE CAN DESTROY EVIDENCE, so the default refuses.
    #
    # 2026-08-07: run exactly as documented, this script wrote 6 rows over
    # a corpus holding 11 and would have deleted every matchday-1 and
    # matchday-2 read. Those rows were the ONLY surviving record of those
    # reads — their snapshots lived in session scratch and died with their
    # containers, which is the fragility #84 fixed forward and could not
    # fix backward. It was caught by diffing before staging, which is a
    # habit rather than a guarantee.
    #
    # The corpus is prospective forecast evidence (AGENTS.md section 6),
    # and "never rewrite historical evidence" is a hard invariant. A tool
    # whose ordinary invocation silently drops it is a defect no amount of
    # care fixes, because the next caller will not diff first.
    ap.add_argument("--merge", action="store_true",
                    help="combine with the existing --out corpus instead of "
                         "replacing it; refuses on a conflicting row")
    ap.add_argument("--replace", action="store_true",
                    help="discard rows the existing --out corpus holds that "
                         "this run does not reproduce. Names every one")
    a = ap.parse_args()
    if a.merge and a.replace:
        print(json.dumps({"error": "merge_and_replace", "built": False,
                          "means": "these ask for opposite things; pick one"}))
        return 2

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
            "capture_time_before_kickoff": True,
            "capture_time_witness": (
                "the document's own `captured_at` when present, else the "
                "file's mtime. captured_at wins because GIT DOES NOT "
                "PRESERVE MTIME — a committed snapshot returns from a "
                "clone with an mtime of the checkout, which would reject "
                "every row. For a committed file the git commit time is "
                "the independent upper bound on the claim"),
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
    if a.out:
        verdict = reconcile_with_existing(Path(a.out), rows,
                                          merge=a.merge, replace=a.replace)
        if verdict.get("refused"):
            print(json.dumps({"built": False, **verdict}, indent=2))
            return 2
        rows = verdict["rows"]
        doc["n_rows"] = len(rows)
        doc["rows"] = rows
        doc["write_mode"] = verdict["write_mode"]
        if verdict.get("preserved") or verdict.get("discarded"):
            doc["existing_corpus"] = {
                k: verdict[k] for k in
                ("preserved", "discarded", "added", "reproduced")
                if k in verdict}

    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}: {len(rows)} rows ({verdict['write_mode']}), "
              f"{len(rejects)} rejected, {len(pending)} pending",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
