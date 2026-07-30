#!/usr/bin/env python3
"""Empirical probe: what EXACTLY does API-Football call a red card?

WHY THIS IS NOT OPTIONAL
    The hunter's conditioning inference keys on the events feed to decide
    whether a violent in-play price move has a visible cause. A red card is
    the primary such cause, and `fixtures/events` reports cards as

        {"type": "Card", "detail": "<something>"}

    where `type` is 'Card' for a booking AND for a dismissal. Only `detail`
    distinguishes them.

    If the string this repo matches on is wrong, red-card detection never
    fires — and then EVERY red-carded match falls through to
    `conditioning_unobserved_stats_available`, which the detector documents as
    the STRONGEST version of the finding. A silently-never-matching string
    would therefore invert the inference in the dangerous direction: the one
    case where the market provably had a reason would be recorded as the case
    where it provably did not.

    That is a `results: 0`-shaped failure — a hollow answer read as an answer
    — so the string is MEASURED here rather than assumed from documentation.

WHAT IT MEASURES
    Recent COMPLETED fixtures in the competitions measured to carry live
    statistics (see hunter_live_stats_coverage_2026-07-29.json), scanning
    every event for card-shaped types and recording the exact `type`/`detail`
    pairs observed, verbatim, with counts.

    A run that finds no dismissal at all is reported as INDETERMINATE, not as
    "the strings are fine".

BUDGET / SECRETS
    Same discipline as scripts/probe_hunter_live_stats.py: hard cap, fixed
    delay, key in a header only, redacted output, PII dropped.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_hunter_live_stats import (ARCHIVE_DIR, REPO_ROOT,  # noqa: E402
                                     BudgetExceeded, Client, _redact,
                                     archive_path, errors_of, load_key,
                                     response_items, sanitise_status)

# the competitions MEASURED to carry live statistics on 2026-07-29
LEAGUES = [(11, "CONMEBOL Sudamericana"), (128, "Liga Profesional Argentina"),
           (71, "Brazil Serie A")]
SEASON = 2026
FIXTURES_PER_LEAGUE = 6
COMPLETED = {"FT", "AET", "PEN"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def probe(key: str, say) -> dict:
    records: list = []
    c = Client(key, records, say)
    out: dict = {"run_utc": _now().isoformat(), "leagues": LEAGUES,
                 "season": SEASON,
                 "purpose": ("measure the provider's verbatim card "
                             "type/detail strings; a guessed string that "
                             "never matches would invert the hunter's "
                             "conditioning inference")}

    status = c.get("status", {}, "status")
    out["status"] = sanitise_status(status)
    say(f"    plan: {json.dumps(out['status'].get('subscription'))}")
    say(f"    quota: {json.dumps(out['status'].get('requests'))}")
    say("")

    # a window that is safely in the past, so statistics/events are posted
    frm = (_now() - timedelta(days=21)).strftime("%Y-%m-%d")
    to = (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    say(f"── completed fixtures {frm} .. {to} ──")

    picked: list = []
    for lid, lname in LEAGUES:
        body = c.get("fixtures", {"league": lid, "season": SEASON,
                                  "from": frm, "to": to},
                     f"fixtures_{lid}")
        items = [it for it in response_items(body) if isinstance(it, dict)]
        done = [it for it in items
                if str((((it.get("fixture") or {}).get("status")) or {})
                       .get("short") or "") in COMPLETED]
        # deterministic: most recent first, so the sample cannot be
        # cherry-picked toward a fixture known to carry a dismissal
        done.sort(key=lambda it: str((it.get("fixture") or {})
                                     .get("date") or ""), reverse=True)
        say(f"    league {lid:<5} {lname[:30]:<30} "
            f"{len(items)} in window, {len(done)} completed, "
            f"taking {min(FIXTURES_PER_LEAGUE, len(done))}")
        for it in done[:FIXTURES_PER_LEAGUE]:
            fx = it.get("fixture") or {}
            picked.append({"fixture_id": fx.get("id"),
                           "date": fx.get("date"),
                           "league_id": lid, "league_name": lname,
                           "provider_errors": errors_of(body)})

    say("")
    say("── scanning events for card-shaped entries ──")
    pairs: dict = {}
    dismissals: list = []
    scanned: list = []
    for p in picked:
        fid = p["fixture_id"]
        if fid is None:
            continue
        try:
            body = c.get("fixtures/events", {"fixture": fid},
                         f"events_{fid}")
        except BudgetExceeded as exc:
            out["budget_abort"] = str(exc)
            say(f"    ABORT: {exc}")
            break
        cards = []
        for ev in response_items(body):
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type") or "").strip()
            detail = str(ev.get("detail") or "").strip()
            pair = f"{etype} / {detail}"
            pairs[pair] = pairs.get(pair, 0) + 1
            if "card" in etype.lower() or "card" in detail.lower():
                t = ev.get("time") or {}
                rec = {"fixture_id": fid, "type": etype, "detail": detail,
                       "elapsed": t.get("elapsed"), "extra": t.get("extra"),
                       "team": (ev.get("team") or {}).get("name"),
                       "team_id": (ev.get("team") or {}).get("id"),
                       "player": (ev.get("player") or {}).get("name")}
                cards.append(rec)
                if "yellow" not in detail.lower() \
                        or "second" in detail.lower():
                    dismissals.append(rec)
        scanned.append({**p, "event_count": len(response_items(body)),
                        "card_events": cards})
        say(f"    fixture {fid:<9} {len(response_items(body)):>3} events, "
            f"{len(cards):>2} card-shaped")
    out["fixtures_scanned"] = scanned
    out["observed_type_detail_pairs"] = dict(
        sorted(pairs.items(), key=lambda kv: -kv[1]))
    out["dismissals_observed"] = dismissals
    out["requests_used"] = c.used
    out["records"] = records
    return out


def main() -> int:
    key, where, problems = load_key()
    lines: list[str] = []

    def say(msg: str = "") -> None:
        text = _redact(msg, key)
        lines.append(text)
        print(text)

    say("API-FOOTBALL RED-CARD STRING PROBE")
    say(f"key source: {where}")
    for p in problems:
        say(f"  !! {p}")
    if not key:
        say("INDETERMINATE — no key; nothing was measured.")
        return 2
    try:
        report = probe(key, say)
    except Exception as exc:
        say(f"INDETERMINATE — {type(exc).__name__}: {_redact(exc, key)}")
        return 2

    say("")
    say("── every type/detail pair observed, verbatim ──")
    for pair, n in report["observed_type_detail_pairs"].items():
        mark = "  <-- CARD" if "card" in pair.lower() else ""
        say(f"    {n:>4}x  {pair!r}{mark}")

    say("")
    dis = report["dismissals_observed"]
    if dis:
        say(f"── {len(dis)} DISMISSAL(S) observed ──")
        seen = sorted({d["detail"] for d in dis})
        for d in dis:
            say(f"    fixture {d['fixture_id']} {d['elapsed']}' "
                f"{d['team']}: type={d['type']!r} detail={d['detail']!r}")
        say("")
        say(f"    DISTINCT dismissal detail strings: {seen}")
        verdict = "MEASURED"
    else:
        say("── NO DISMISSAL OBSERVED ──")
        say("    INDETERMINATE: this run did not see a red card, so it")
        say("    proves nothing about the dismissal string. It is NOT")
        say("    evidence that the matcher is correct.")
        verdict = "INDETERMINATE_NO_DISMISSAL_SEEN"
    report["verdict"] = verdict
    say("")
    say(f"verdict: {verdict}")
    say(f"requests used: {report['requests_used']}")

    report["stdout"] = lines
    path = archive_path(_now(), what="redcard_strings")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    blob = _redact(json.dumps(report, indent=1, sort_keys=True,
                              default=str), key)
    path.write_text(blob, encoding="utf-8")
    print(f"archived -> {path.relative_to(REPO_ROOT)} "
          f"({len(blob) / 1024:.0f} KB)")
    return 0 if verdict == "MEASURED" else 2


if __name__ == "__main__":
    sys.exit(main())
