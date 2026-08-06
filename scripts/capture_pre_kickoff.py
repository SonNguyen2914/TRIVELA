#!/usr/bin/env python3
"""Freeze a pre-kickoff read snapshot somewhere it will survive.

    python scripts/capture_pre_kickoff.py --competition leagues-cup

WHY THIS EXISTS. `scripts/build_prospective_corpus.py` pairs reads
observed BEFORE kickoff with settled outcomes, and the clean arm of the
hosting-offset measurement consumes the result. Both slates so far were
captured only into a session scratch directory that dies with the
container: the corpus for 2026-08-04/05 survived by luck, not design.
A snapshot nobody can find later is a measurement that never happened.

This writes the snapshot into `research_archive/pre_kickoff/`, which is
tracked, so committing it makes it durable.

THE TRAP THAT MAKES "JUST COMMIT IT" INSUFFICIENT. The corpus builder's
second admissibility condition is that the snapshot FILE was written
before kickoff, and its only witness was the file's mtime. **Git does
not preserve mtime.** After any clone or checkout every file's mtime
becomes the checkout time — i.e. now — which is after every past
kickoff, so a committed snapshot would have had all of its rows
rejected. Verified in this repo: a document committed 2026-08-05
04:22 PDT carries an mtime of 2026-08-05 11:22 in a fresh container.

So the capture writes `captured_at` INTO the document, from the clock at
fetch time, and the builder prefers that over mtime. For a committed
file the git commit time is then an independent upper bound an auditor
can check the claim against — a stronger witness than mtime ever was,
because it cannot be back-dated by touching a file.

A capture is IMMUTABLE: an existing path is never overwritten. Two
captures of the same slate are two files, and the builder keeps the one
closest to kickoff.
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

DEFAULT_DIR = Path("research_archive/pre_kickoff")
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


def summarise(doc: dict, now: datetime) -> dict:
    """What this capture will and will not contribute, stated up front
    so a zero-yield capture is obvious at the moment it is taken rather
    than at settlement, when it is too late to take another."""
    usable, started, no_read = [], [], []
    for f in (doc.get("fixtures") or []):
        name = (f"{(f.get('home') or {}).get('name')} v "
                f"{(f.get('away') or {}).get('name')}")
        ko = _utc(f.get("kickoff_utc"))
        g = f.get("goals") or {}
        if ko is None or ko <= now or f.get("status") not in NOT_STARTED \
                or g.get("home") is not None:
            started.append(name)
            continue
        s = f.get("strength") or {}
        if not s.get("available"):
            no_read.append(name)
            continue
        usable.append({"fixture": name,
                       "lead_minutes": int((ko - now).total_seconds() // 60)})
    return {"usable": usable, "n_usable": len(usable),
            "already_started_or_past": started,
            "no_strength_read": no_read}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition", required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--now", default=None,
                    help="override the capture clock (tests only)")
    a = ap.parse_args()

    if not a.api_base:
        print(json.dumps({"error": "no_api_base", "captured": False}))
        return 2

    now = _utc(a.now) or datetime.now(timezone.utc)
    u = (f"{a.api_base}/api/comp/{a.competition}/fixtures"
         f"?days={a.days}&include_finished=true")
    doc = json.load(urllib.request.urlopen(u))

    stamp = now.strftime("%Y-%m-%dT%H%MZ")
    out = Path(a.dir) / f"{a.competition}-{stamp}.json"
    if out.exists():
        print(json.dumps({
            "error": "capture_exists", "captured": False, "path": str(out),
            "means": ("a capture is immutable — overwriting one would "
                      "silently rewrite the evidence a later measurement "
                      "cites. Wait a minute or pass a different --dir")}))
        return 2

    summary = summarise(doc, now)
    doc["captured_at"] = now.isoformat()
    doc["captured_by"] = "scripts/capture_pre_kickoff.py"
    doc["captured_at_is_the_witness"] = (
        "the corpus builder's pre-kickoff condition reads THIS field, not "
        "the file's mtime: git does not preserve mtime, so a committed "
        "snapshot would otherwise have every row rejected after a clone. "
        "For a committed file the git commit time is the independent "
        "upper bound to check this claim against")
    doc["capture_summary"] = summary

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    print(json.dumps({
        "captured": True, "path": str(out), "captured_at": now.isoformat(),
        "n_usable": summary["n_usable"],
        "usable": summary["usable"],
        "no_strength_read": summary["no_strength_read"],
        "already_started_or_past": len(summary["already_started_or_past"]),
        "commit_it": ("this path is tracked; commit the file or it dies "
                      "with the container, which is the failure this "
                      "script exists to end"),
    }, indent=2))
    return 0 if summary["n_usable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
