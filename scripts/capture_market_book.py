#!/usr/bin/env python3
"""Freeze a market book snapshot so book behaviour can be measured later.

    python scripts/capture_market_book.py --competition leagues-cup

WHY THIS EXISTS. The read snapshots are durable since #84; the BOOKS
beside them are not. Every market snapshot this project has ever taken
lives in a session scratch directory that dies with its container, so
the first attempt to measure how books move before kickoff — vig
compression, price drift, where the liquidity arrives — rested on
artifacts that would not survive the week. The measurement is only
possible at all because a container happened not to have been reclaimed.

That is the identical fragility #84 fixed for fixture reads, and it is
filed as a gap on issue #68. This closes it for books.

WHAT IT DOES NOT DO. It predicts nothing and recommends nothing. It is
an input. `scripts/measure_book_timing.py` reads these and refuses to
draw a conclusion until there are enough of them.

THE WITNESS, same design and same reason as #84. `captured_at` is
written into the document, because git does not preserve mtime: a
committed snapshot returns from a clone with an mtime of the checkout,
and any analysis keyed on file time would silently mis-order the whole
series. For a committed file the git commit time is an independent
upper bound on the claim.

WHAT IS STORED, and what deliberately is not. Only what a book-behaviour
measurement needs: ticker, the three legs, ask/bid, open interest,
volume, and the derived sum-of-asks. No account data, no journal rows,
no operator information — this is public exchange state and nothing
else, so it can live in the archive without a redaction question.

A capture is IMMUTABLE: an existing path is never overwritten. Two
captures a minute apart are two files, which is the point — the series
IS the measurement.
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

DEFAULT_DIR = Path("research_archive/books")


def _utc(ts: str | None):
    if not ts:
        return None
    t = ts.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def condense(markets_doc: dict, fixtures_doc: dict | None, now: datetime):
    """One row per event: the three legs, and the kickoff when a fixture
    payload is supplied so lead time is recoverable without re-fetching.

    Events with anything other than three legs are KEPT and flagged
    rather than dropped — a two-leg book is a different object, and a
    measurement that silently skipped them would understate how often
    the exchange's shape changes.
    """
    ko = {}
    for f in ((fixtures_doc or {}).get("fixtures") or []):
        if f.get("kalshi_event"):
            ko[f["kalshi_event"]] = {
                "kickoff_utc": f.get("kickoff_utc"),
                "home": (f.get("home") or {}).get("name"),
                "away": (f.get("away") or {}).get("name"),
                "status": f.get("status"),
            }
    rows = []
    for e in (markets_doc.get("events") or []):
        legs = []
        for r in (e.get("markets") or []):
            legs.append({
                "ticker": r.get("ticker"),
                "label": r.get("no_sub_title") or r.get("yes_sub_title"),
                "yes_ask": _f(r.get("yes_ask_dollars")),
                "yes_bid": _f(r.get("yes_bid_dollars")),
                "open_interest": _f(r.get("open_interest_fp")),
                "volume": _f(r.get("volume")),
            })
        asks = [l["yes_ask"] for l in legs if l["yes_ask"] is not None]
        meta = ko.get(e.get("event_ticker")) or {}
        k = _utc(meta.get("kickoff_utc"))
        rows.append({
            "event_ticker": e.get("event_ticker"),
            "legs": legs,
            "n_legs": len(legs),
            "three_way": len(legs) == 3,
            "sum_of_asks": round(sum(asks), 4) if asks else None,
            "vig": round(sum(asks) - 1, 4) if asks else None,
            **meta,
            "lead_seconds": (int((k - now).total_seconds())
                             if k else None),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition", required=True)
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--now", default=None, help="override clock (tests only)")
    # Backfill. Observations already taken into scratch are real
    # observations of real books at real times, and discarding them is
    # the very loss this script exists to prevent — so they can be
    # imported. They are marked `scratch_backfill`, and their
    # `captured_at` is the caller's claim rather than this script's own
    # clock, which the document records so nobody later mistakes an
    # imported point for a first-hand one.
    ap.add_argument("--from-markets", default=None,
                    help="local markets payload instead of fetching")
    ap.add_argument("--from-fixtures", default=None)
    a = ap.parse_args()

    backfill = bool(a.from_markets)
    if backfill and not a.now:
        print(json.dumps({"error": "backfill_needs_now", "captured": False,
                          "means": ("an imported snapshot has no capture "
                                    "time of its own; supply --now with "
                                    "the moment it was actually taken")}))
        return 2
    if not backfill and not a.api_base:
        print(json.dumps({"error": "no_api_base", "captured": False}))
        return 2

    now = _utc(a.now) or datetime.now(timezone.utc)
    if backfill:
        mk = json.loads(Path(a.from_markets).read_text())
        fx = (json.loads(Path(a.from_fixtures).read_text())
              if a.from_fixtures else None)
    else:
        mk = json.load(urllib.request.urlopen(
            f"{a.api_base}/api/comp/{a.competition}/markets"))
        try:
            fx = json.load(urllib.request.urlopen(
                f"{a.api_base}/api/comp/{a.competition}/fixtures"
                "?days=3&include_finished=true"))
        except Exception:
            fx = None      # kickoffs are a convenience, not a requirement

    rows = condense(mk, fx, now)
    stamp = now.strftime("%Y-%m-%dT%H%MZ")
    out = Path(a.dir) / f"{a.competition}-books-{stamp}.json"
    if out.exists():
        print(json.dumps({
            "error": "capture_exists", "captured": False, "path": str(out),
            "means": ("a capture is immutable — overwriting one would "
                      "rewrite a point in the very series it belongs to")}))
        return 2

    doc = {
        "what_this_is": ("a frozen public market book. An INPUT to "
                         "scripts/measure_book_timing.py, which predicts "
                         "nothing until it has enough of these"),
        "what_this_is_NOT": ("not an edge claim, not a recommendation, "
                             "not a forecast of where the crowd goes"),
        "captured_at": now.isoformat(),
        "captured_by": "scripts/capture_market_book.py",
        "scratch_backfill": backfill,
        "captured_at_provenance": (
            "caller-supplied: this snapshot was imported from a scratch "
            "payload and `captured_at` is the importer's claim about when "
            "it was taken, NOT this script's own clock"
            if backfill else
            "first-hand: this script's clock at the moment it fetched"),
        "captured_at_is_the_witness": (
            "analysis orders this series by THIS field, not file mtime: "
            "git does not preserve mtime, so a committed snapshot returns "
            "from a clone stamped with the checkout time and the whole "
            "series would silently mis-order. For a committed file the "
            "git commit time is the independent upper bound"),
        "contains_only_public_exchange_state": True,
        "competition": a.competition,
        "n_events": len(rows),
        "n_three_way": sum(1 for r in rows if r["three_way"]),
        "events": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    upcoming = [r for r in rows
                if r.get("lead_seconds") and r["lead_seconds"] > 0]
    print(json.dumps({
        "captured": True, "path": str(out), "captured_at": now.isoformat(),
        "n_events": len(rows), "n_three_way": doc["n_three_way"],
        "scratch_backfill": backfill,
        "n_upcoming_with_kickoff": len(upcoming),
        "commit_it": ("this path is tracked; commit the file or it dies "
                      "with the container, which is the failure this "
                      "script exists to end"),
    }, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
