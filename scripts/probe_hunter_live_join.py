#!/usr/bin/env python3
"""End-to-end probe: does the Kalshi -> API-Football join actually resolve
against REAL names and REAL live fixtures?

WHY THIS IS SEPARATE FROM THE UNIT TESTS
    `tests/test_hunter_live_stats.py` proves the join fails closed on
    canned payloads: ambiguity refuses, an unmapped series refuses, codes
    that match nothing refuse. What it CANNOT prove is that the heuristic
    resolves a real Kalshi event against the provider's real team names,
    because both sides' naming is the provider's to change.

    So this probe drives the real path: real Kalshi open markets for the
    mapped series, the real `fixtures?live=all` index, and the real
    resolver. It reports, per event, whether it resolved and — when it did
    not — WHICH fail-closed step it stopped at.

WHAT A CLEAN RUN DOES AND DOES NOT ESTABLISH
    It establishes that the join can resolve, and it shows the resolution
    rate on the slate that happened to be live. It does NOT establish that
    fixture identity is solved: the join is name-based, which this repo
    forbids for a MODEL feature, and a run with no ambiguous case
    encountered says nothing about how ambiguity behaves in the wild.
    Read it as one slate, on one day.

BUDGET / SECRETS
    Same discipline as the other probes: hard cap, fixed delay, key in a
    header only, redacted output, PII dropped, non-clobbering archive.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from probe_hunter_live_stats import (ARCHIVE_DIR, REPO_ROOT,  # noqa: E402
                                     _redact, archive_path, load_key)
from src.live import apifootball_live as al  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def kalshi_open_events(series: str) -> tuple[dict, int]:
    """Open markets for one series, grouped by event ticker. One request."""
    r = requests.get(f"{KALSHI}/markets",
                     params={"series_ticker": series, "status": "open",
                             "limit": 200}, timeout=20)
    r.raise_for_status()
    body = r.json()
    by_event: dict[str, list[str]] = {}
    for m in (body.get("markets") or []):
        ev, tick = m.get("event_ticker"), m.get("ticker")
        if not ev or not tick:
            continue          # no provider-stable identity, no candidate
        by_event.setdefault(ev, []).append(tick)
    return by_event, len(body.get("markets") or [])


def main() -> int:
    key, where, problems = load_key()
    lines: list[str] = []

    def say(msg: str = "") -> None:
        text = _redact(msg, key)
        lines.append(text)
        print(text)

    say("HUNTER LIVE-JOIN END-TO-END PROBE")
    say(f"key source: {where}")
    for p in problems:
        say(f"  !! {p}")
    if not key:
        say("INDETERMINATE — no key; nothing was measured.")
        return 2

    report: dict = {"run_utc": _now().isoformat(),
                    "competition_map": al.COMPETITION_MAP,
                    "competition_map_fingerprint":
                        al.competition_map_fingerprint()}
    say(f"competition map fingerprint: "
        f"{al.competition_map_fingerprint()[:16]}…")
    say(f"mapped series: {sorted(al.COMPETITION_MAP)}")
    say("")

    # the provider side: one request for every in-play fixture on earth
    client = al.LiveClient(key, cycle_cap=40)
    try:
        index = al.fetch_live_index(client)
    except Exception as exc:
        say(f"INDETERMINATE — live index: {type(exc).__name__}: "
            f"{_redact(exc, key)}")
        return 2
    say(f"live fixtures worldwide: {index['fixtures_live_total']} "
        f"across {len(index['by_league'])} competitions")
    if index.get("count_disagreement"):
        say(f"  !! {index['count_disagreement']}")
    for series, m in sorted(al.COMPETITION_MAP.items()):
        n = len(index["by_league"].get(m["league_id"]) or [])
        say(f"  {series:<22} league {m['league_id']:<5} "
            f"{m['league_name'][:28]:<28} {n} live now")
    say("")

    # the Kalshi side, per mapped series
    say("── resolving real Kalshi events against the live index ──")
    results: list = []
    for series in sorted(al.COMPETITION_MAP):
        try:
            by_event, n_markets = kalshi_open_events(series)
        except Exception as exc:
            say(f"  {series}: Kalshi read failed "
                f"({type(exc).__name__}) — recorded, not skipped")
            results.append({"series": series,
                            "kalshi_error": type(exc).__name__})
            continue
        say(f"  {series}: {n_markets} open markets in "
            f"{len(by_event)} events")
        for ev, tickers in sorted(by_event.items()):
            join = al.resolve_fixture(series, tickers, index)
            row = {"series": series, "event_ticker": ev,
                   "leg_tickers": tickers,
                   "team_codes": join.get("kalshi_team_codes"),
                   "status": join["status"],
                   "detail": join.get("detail"),
                   "fixture_id": join.get("fixture_id"),
                   "matched": (f"{join.get('home_name')} vs "
                               f"{join.get('away_name')}")
                   if join["status"] == al.JOIN_RESOLVED else None,
                   "elapsed": join.get("elapsed")}
            results.append(row)
            mark = "RESOLVED" if join["status"] == al.JOIN_RESOLVED else "—"
            say(f"    {ev:<42} codes={str(join.get('kalshi_team_codes')):<16}"
                f" {mark:<9} {join['status']}"
                + (f"  -> {row['matched']} @{join.get('elapsed')}'"
                   if row["matched"] else ""))

    resolved = [r for r in results if r.get("status") == al.JOIN_RESOLVED]
    say("")
    say(f"── {len(resolved)} event(s) resolved ──")

    # for each resolved fixture, take a real reading and classify
    classifications: list = []
    for r in resolved:
        try:
            reading = al.fetch_reading(client, r["fixture_id"])
        except Exception as exc:
            say(f"  reading {r['fixture_id']} failed: "
                f"{type(exc).__name__}")
            continue
        join = {"status": al.JOIN_RESOLVED, "fixture_id": r["fixture_id"],
                "league_id": None, "orientation": "home_away",
                "elapsed": r["elapsed"], "goals": {}}
        cond = al.classify_conditioning(reading, join,
                                        elapsed=r["elapsed"],
                                        window_minutes=12)
        xg = al.live_xg_characterisation(reading, join)
        st = reading.get("statistics") or {}
        ev = reading.get("events") or {}
        classifications.append({
            "event_ticker": r["event_ticker"],
            "fixture_id": r["fixture_id"], "elapsed": r["elapsed"],
            "statistic_types": st.get("statistic_type_count_max"),
            "events": ev.get("event_count"),
            "goals_seen": ev.get("goal_count"),
            "reds_seen": ev.get("red_card_count"),
            "conditioning_state": cond["state"],
            "strength_rank": cond["strength_rank"],
            "reason": cond["reason"],
            "xg_available": xg.get("available"),
            "xg_differential": xg.get("xg_differential_first_minus_second"),
        })
        c = classifications[-1]
        say(f"  {r['event_ticker']:<42} {c['statistic_types']:>3} types "
            f"{c['events']:>3} events  goals={c['goals_seen']} "
            f"reds={c['reds_seen']}  -> {c['conditioning_state']} "
            f"(rank {c['strength_rank']})")

    report["live_index"] = {
        "fixtures_live_total": index["fixtures_live_total"],
        "competitions": len(index["by_league"]),
        "per_mapped_series": {
            s: len(index["by_league"].get(m["league_id"]) or [])
            for s, m in al.COMPETITION_MAP.items()}}
    report["join_results"] = results
    report["classifications"] = classifications
    report["requests_used"] = client.used
    report["budget"] = client.budget_block()
    report["what_this_does_not_establish"] = (
        "the join is name-based and this run is ONE slate on ONE day. A "
        "run with no ambiguous case says nothing about how ambiguity "
        "behaves in the wild, and fixture-level identity remains unproven "
        "in this repo (docs/APIFOOTBALL-TRIAL.md section 8.6). Resolution "
        "here licenses an OBSERVATIONAL row and nothing more")

    say("")
    say(f"requests used: {client.used} (API-Football) + "
        f"{len(al.COMPETITION_MAP)} (Kalshi)")
    say(f"provider quota remaining today: {client.quota_remaining_day}")

    report["stdout"] = lines
    path = archive_path(_now(), what="join_e2e")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    blob = _redact(json.dumps(report, indent=1, sort_keys=True,
                              default=str), key)
    path.write_text(blob, encoding="utf-8")
    print(f"archived -> {path.relative_to(REPO_ROOT)} "
          f"({len(blob) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
