#!/usr/bin/env python3
"""Compare the journal against the operator's actual account.

    python scripts/reconcile_journal.py \
        --competition leagues-cup --positions positions.json

WHY THIS EXISTS. Nothing ever checked the journal against reality, and
on 2026-08-05 that cost three separate ways in one night:

  - a $35 position on FC Dallas that returned $70.35 — the best result
    of the slate — had NO journal row at all. It appeared in no
    briefing tally, no betting window and no debrief. Had it lost,
    nothing would have recorded that either;
  - a $35 matchday-1 position on Vancouver was invisible for two days,
    and the matchday-1 debrief was written on the assumption that no
    position was held that slate;
  - two rows read `passed` — a word asserting the operator DECLINED —
    while real money sat on both fixtures.

Every one of those is a comparison nobody was making. This makes it.

WHAT IT DOES NOT DO. It writes nothing, resolves nothing and corrects
nothing. It reports four disagreement classes and names every row and
position involved; acting on them is the operator's, through the
journal's own correction path. A tool that silently repaired the record
to match an account statement would destroy the very audit trail the
`corrects_bet_id` chain exists to preserve.

MATCHING IS BY MARKET TICKER, never by club name and never by name+date
(AGENTS.md section 13). A position whose ticker matches no row is
reported as unmatched rather than attached to the nearest plausible
fixture — the whole point is to surface gaps, so a fuzzy match here
would hide exactly what the tool is for.

The positions file is a list of what the account actually holds or held:

    [{"market_ticker": "KXLEAGUESCUPGAME-26AUG05DALQUE-DAL",
      "cost": 35.00, "paid_out": 70.35, "entry_price": 0.48,
      "settled_at": "2026-08-05T20:03:00-07:00"}]

`cost` is required; everything else is optional and only sharpens the
comparison.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A row in one of these states asserts that no money was placed. Finding
# a real position against one is the class that produced two false
# records on 2026-08-05, so it is reported separately from a plain
# size or price disagreement rather than folded in with them.
NO_MONEY_STATES = {"passed", "considered"}

# Derived all-in prices (cost / payout) run about a cent above the raw
# entry because they include fees, so a price comparison must tolerate
# at least that much before it means anything.
PRICE_TOLERANCE = 0.02
SIZE_TOLERANCE = 0.01


def fetch_entries(base: str, competition: str) -> tuple[list, list, dict]:
    """Every journal row for a competition, across all its scopes.

    Also returns whether the SERVER could supply rows at all. An empty
    `entries` list is indistinguishable from "this competition has no
    rows" unless the counts are checked against it, and the difference
    is the whole result: run against a server predating the row listing,
    this script reported all seven of a real slate's positions as having
    no journal row. Six of them had one. A tool that turns a stale
    deployment into seven fabricated findings is worse than no tool.
    """
    u = f"{base}/api/comp/{competition}/journal"
    doc = json.load(urllib.request.urlopen(u))
    rows, scopes = [], doc.get("scopes_found") or []
    recorded = 0
    listing = True
    for slug in scopes:
        sc = ((doc.get("scopes") or {}).get(slug) or {})
        recorded += int(sc.get("total_recorded") or 0)
        if "entries" not in sc:
            listing = False
        for e in (sc.get("entries") or []):
            e = dict(e)
            e["competition_slug"] = slug
            rows.append(e)
    health = {"total_recorded": recorded, "entries_returned": len(rows),
              "server_lists_rows": listing}
    return rows, scopes, health


def refusal(health: dict) -> dict | None:
    """Refuse rather than report phantom gaps."""
    if not health["server_lists_rows"]:
        return {"refused": ("the journal surface returned no `entries` "
                            "key. Every position would be reported as "
                            "having no row, which would be false"),
                "likely_cause": ("the deployed API predates the row "
                                 "listing"),
                **health}
    if health["total_recorded"] and not health["entries_returned"]:
        return {"refused": (f"the journal reports "
                            f"{health['total_recorded']} recorded rows and "
                            f"returned none"),
                **health}
    return None


def reconcile(entries: list, positions: list) -> dict:
    by_ticker: dict[str, list] = {}
    for e in entries:
        by_ticker.setdefault(e.get("market_ticker") or "", []).append(e)

    matched, contradictions, mismatches = [], [], []
    unmatched_positions = []
    seen_tickers = set()

    for p in positions:
        t = p.get("market_ticker") or ""
        rows = by_ticker.get(t) or []
        if not rows:
            unmatched_positions.append({
                "market_ticker": t, "cost": p.get("cost"),
                "why": ("real money on a market with NO journal row. This "
                        "position exists only in the account"),
            })
            continue
        seen_tickers.add(t)
        # A correction chain: judge the row that is NOT superseded.
        live = [r for r in rows if not r.get("superseded")] or rows
        r = sorted(live, key=lambda x: x.get("id") or 0)[-1]
        rec = {"market_ticker": t, "bet_id": r.get("id"),
               "status": r.get("status"), "cost": p.get("cost")}

        if r.get("status") in NO_MONEY_STATES:
            contradictions.append({
                **rec,
                "why": (f"row says {r['status']!r} — which asserts no "
                        f"money was placed — but the account shows a "
                        f"position costing {p.get('cost')}"),
            })
            continue

        deltas = {}
        # `stated_size` is redacted from the public journal — it is
        # staking, and that surface takes no credential. So a size
        # disagreement (the $33.54-vs-$35.00 class) is NOT detectable
        # from a public feed, and the report says so by name below
        # rather than letting silence read as agreement.
        size = _num(r.get("stated_size"))
        cost = _num(p.get("cost"))
        if size is not None and cost is not None \
                and abs(size - cost) > SIZE_TOLERANCE:
            deltas["size"] = {"row": size, "account": cost}
        price = _num(r.get("stated_price_dollars"))
        entry = _num(p.get("entry_price"))
        if price is not None and entry is not None \
                and abs(price - entry) > PRICE_TOLERANCE:
            deltas["price"] = {"row": price, "account": entry,
                               "tolerance": PRICE_TOLERANCE}
        if deltas:
            mismatches.append({**rec, "deltas": deltas})
        else:
            matched.append(rec)

    # An orphan's status decides whether it is a finding at all. A
    # `passed`, `considered` or `void` row with no position is the
    # expected shape — the operator declined, or the row is late
    # documentation. A `taken` row with no position is the mirror image
    # of the contradiction above: the journal claims money was placed
    # and the account shows none. Lumping the two together let a first
    # production run report `clean: true` while five `taken` matchday-1
    # rows sat unmatched, which is the false-clean this tool exists to
    # prevent.
    rows_without_position, claimed_without_position = [], []
    for e in entries:
        if (e.get("market_ticker") or "") in seen_tickers \
                or e.get("superseded"):
            continue
        rec = {"bet_id": e.get("id"),
               "market_ticker": e.get("market_ticker"),
               "status": e.get("status")}
        if e.get("status") == "taken":
            claimed_without_position.append({
                **rec,
                "why": ("row says `taken` — money WAS placed — but the "
                        "supplied account shows no position on this "
                        "market. Either the row is wrong, or the "
                        "positions file is incomplete"),
            })
        else:
            rows_without_position.append({
                **rec,
                "why": ("recorded, and the account shows no position. "
                        "Expected for a genuine pass or for late "
                        "documentation"),
            })

    return {
        "what_this_is": (
            "a comparison between the journal and the operator-supplied "
            "account, by market ticker. It reports disagreements and "
            "writes nothing"),
        "what_this_is_NOT": (
            "not a repair, not an edge claim, not P&L. Acting on any "
            "finding here goes through the journal's own correction "
            "path so the mistake stays visible beside its fix"),
        "n_journal_entries": len(entries),
        "n_account_positions": len(positions),
        "agreed": matched,
        "positions_with_no_journal_row": unmatched_positions,
        "journal_rows_with_no_position": rows_without_position,
        "taken_rows_with_no_position": claimed_without_position,
        "status_contradictions": contradictions,
        "value_mismatches": mismatches,
        "clean": not (unmatched_positions or contradictions or mismatches
                      or claimed_without_position),
        "matching": {
            "key": "market_ticker",
            "why": ("provider-stable identity. Names and name+date are "
                    "refused: the same clubs meet twice in this "
                    "competition, and a fuzzy match would hide the gaps "
                    "this tool exists to surface"),
        },
        "price_tolerance": PRICE_TOLERANCE,
        "price_tolerance_why": (
            "a price derived from cost/payout is fee-inclusive and runs "
            "about a cent above the raw entry, so a smaller difference "
            "is not evidence of anything"),
        # A comparison that could not run must be NAMED. Silence here
        # would read as agreement, which is the exact failure mode this
        # tool was built to end.
        "comparisons_not_run": _not_run(entries),
    }


def _not_run(entries: list) -> list:
    """Which checks the supplied rows could not support, and why."""
    out = []
    if entries and not any("stated_size" in e for e in entries):
        out.append({
            "check": "size",
            "why": ("no row carried `stated_size`. The public journal "
                    "redacts it as staking, so a size disagreement is "
                    "invisible from that surface"),
            "how_to_run_it": ("reconcile against an operator-credentialed "
                              "journal route, which carries the full row"),
        })
    if entries and not any(e.get("stated_price_dollars") for e in entries):
        out.append({
            "check": "price",
            "why": "no row carried a stated price",
            "how_to_run_it": "record entries with the price they cite",
        })
    return out


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition", required=True,
                    help="viewer competition key, e.g. leagues-cup")
    ap.add_argument("--positions", required=True,
                    help="JSON list of account positions")
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if not a.api_base:
        print(json.dumps({"error": "no_api_base", "reconciled": False}))
        return 2
    positions = json.loads(Path(a.positions).read_text())
    if isinstance(positions, dict):
        positions = positions.get("positions") or []

    entries, scopes, health = fetch_entries(a.api_base, a.competition)
    bad = refusal(health)
    if bad:
        print(json.dumps({"competition": a.competition, "scopes": scopes,
                          "reconciled": False, **bad}, indent=2))
        return 2
    doc = reconcile(entries, positions)
    doc["competition"] = a.competition
    doc["scopes"] = scopes
    doc["source_health"] = health

    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    else:
        print(text)
    # A disagreement is a finding, not a crash: exit 1 so a caller can
    # branch on it, and 0 only when the two records actually agree.
    return 0 if doc["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
