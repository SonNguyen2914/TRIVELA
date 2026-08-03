#!/usr/bin/env python
"""Record a personal view against a fixture — the pick-loop's write path.

Assembling a journal write by hand takes three lookups (the internal
fixture id, a citable quote id, the contract ticker) and gets one of them
wrong under time pressure. On 2026-08-01 a whole 14-fixture slate went
unrecorded and eleven of those fixtures became permanently unrecordable,
because `record_view` stamps `void` on anything cited after kickoff. This
script does the lookups from ONE briefing call and refuses the writes
that would have been silently worthless.

    # what can I cite, and how long have I got?
    python scripts/journal_pick.py show 761696

    # record a view (needs JOURNAL_TOKEN)
    python scripts/journal_pick.py record 761696 home_win --rationale "..."

    # resolve it once you have or have not placed the bet
    python scripts/journal_pick.py resolve 1234 taken

`show` needs no credential and is the half you run first.

THREE REFUSALS, all of them lessons rather than politeness:

  - past kickoff, `record` stops. The server would accept the row and
    mark it `void` — keepable, counted nowhere. A silent void reads like
    a successful write in a terminal.
  - a quote older than JOURNAL_QUOTE_MAX_AGE_SECONDS (900s) downgrades
    the entry to `stated_only`, which "is recorded honestly and counted
    nowhere". `record` warns and requires --allow-stated-only, so the
    weaker basis is a decision instead of an accident.
  - no token, no write. Never falls back to ADMIN_TOKEN, which arms
    model approvals and does not belong in a pick-loop session.

This script places no orders and names no side to back. It records a
view the operator has already formed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get(
    "TRIVELA_API_BASE",
    "https://wc26-bet-suggester-production.up.railway.app").rstrip("/")
TOKEN = os.environ.get("JOURNAL_TOKEN", "").strip()

# server-side ceiling past which a cited quote stops granting
# observed_quote (config.JOURNAL_QUOTE_MAX_AGE_SECONDS). Mirrored here to
# warn BEFORE the write rather than explain the downgrade after it.
QUOTE_MAX_AGE_S = 900
THREE_WAY = ("home_win", "draw", "away_win")


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _post(path: str, body: dict) -> dict:
    if not TOKEN:
        sys.exit("JOURNAL_TOKEN is not set — refusing to attempt a write.\n"
                 "Set it in the environment; do NOT substitute ADMIN_TOKEN.")
    req = urllib.request.Request(
        f"{BASE}{path}", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Journal-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        sys.exit(f"HTTP {e.code} from {path}: {detail}")


def _now():
    return datetime.now(timezone.utc)


def _kickoff(brief: dict):
    raw = (brief.get("fixture") or {}).get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _citable(brief: dict) -> dict:
    """outcome_key -> the NEWEST citable quote for it.

    Newest wins, from either source, because freshness is what grants
    `observed_quote`: an older quote is refused as the price basis past
    the 900s ceiling however authoritative it looks. Around the T-10
    sweep the frozen lock quote IS the newest, which is why recording in
    that window lines an entry up with the same book the platform's own
    evidence uses — and with what CLV is later measured against.
    """
    out: dict = {}
    frozen = (brief.get("market_frozen_t10") or {}).get("contracts") or []
    for c in frozen:
        k = c.get("outcome_key")
        if k and c.get("market_quote_id") is not None:
            out[k] = {"market_quote_id": c["market_quote_id"],
                      "ticker": c.get("ticker"),
                      "captured_at": c.get("quote_captured_at"),
                      "source": "market_frozen_t10",
                      "ask": c.get("frozen_yes_ask_dollars"),
                      "bid": c.get("frozen_yes_bid_dollars"),
                      "model_probability": c.get("model_probability")}
    for q in (brief.get("market_persisted") or {}).get("quotes") or []:
        k = q.get("outcome_key")
        if not k or q.get("market_quote_id") is None:
            continue
        prev = out.get(k)
        if prev is None or (q.get("captured_at") or "") > (
                prev.get("captured_at") or ""):
            out[k] = {"market_quote_id": q["market_quote_id"],
                      "ticker": q.get("ticker"),
                      "captured_at": q.get("captured_at"),
                      "source": "market_persisted",
                      "status": q.get("status"),
                      "age_seconds": q.get("age_seconds")}
    return out


def _age(captured_at: str | None):
    if not captured_at:
        return None
    try:
        t = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((_now() - t).total_seconds())


def cmd_show(args) -> int:
    brief = _get(f"/api/mls/briefing/{args.event_id}")
    if brief.get("error"):
        sys.exit(f"briefing unavailable: {brief['error']}")
    fx = brief.get("fixture") or {}
    ko = _kickoff(brief)
    home = (fx.get("home") or {}).get("abbrev")
    away = (fx.get("away") or {}).get("abbrev")
    print(f"{home} v {away}   espn={args.event_id}   "
          f"fixture_id={brief.get('fixture_id')}")
    if ko:
        mins = (ko - _now()).total_seconds() / 60
        if mins >= 0:
            print(f"kickoff {ko:%Y-%m-%d %H:%M}Z — {mins:.0f} min away")
        else:
            print(f"kickoff {ko:%Y-%m-%d %H:%M}Z — STARTED {-mins:.0f} min "
                  f"ago. A view recorded now is stamped `void`.")
    cite = _citable(brief)
    if not cite:
        print("\nNo citable quote for any outcome. An entry recorded now "
              "would be `stated_only` and would count nowhere.")
        return 0
    print(f"\n{'outcome':<10} {'quote_id':>9} {'age':>8} {'basis granted':<15} "
          f"{'ticker'}")
    print("-" * 78)
    for k in THREE_WAY:
        c = cite.get(k)
        if not c:
            print(f"{k:<10} {'-':>9} {'-':>8} {'stated_only':<15} "
                  f"(no citable quote)")
            continue
        age = _age(c.get("captured_at"))
        ok = age is not None and age <= QUOTE_MAX_AGE_S
        basis = "observed_quote" if ok else "stated_only"
        astr = f"{age}s" if age is not None else "?"
        print(f"{k:<10} {c['market_quote_id']:>9} {astr:>8} {basis:<15} "
              f"{c.get('ticker')}")
    stale = [k for k in THREE_WAY
             if k in cite and (_age(cite[k].get("captured_at")) or 10 ** 9)
             > QUOTE_MAX_AGE_S]
    if stale:
        print(f"\nquotes past the {QUOTE_MAX_AGE_S}s ceiling: "
              f"{', '.join(stale)} — the id stays citable but the price "
              f"basis downgrades. The T-10 sweep refreshes these ~10 min "
              f"before kickoff.")
    return 0


def cmd_record(args) -> int:
    brief = _get(f"/api/mls/briefing/{args.event_id}")
    if brief.get("error"):
        sys.exit(f"briefing unavailable: {brief['error']}")
    fixture_id = brief.get("fixture_id")
    if fixture_id is None:
        sys.exit("briefing carries no fixture_id — cannot record. "
                 "record_view needs the INTERNAL id, never the ESPN one.")
    ko = _kickoff(brief)
    if ko is not None and _now() >= ko and not args.allow_void:
        sys.exit(f"kickoff was {(_now()-ko).total_seconds()/60:.0f} min ago. "
                 f"The server would record this as `void` — kept, counted "
                 f"nowhere. Pass --allow-void to record it anyway as "
                 f"documentation.")
    cite = _citable(brief).get(args.outcome_key)
    quote_id = args.market_quote_id or (cite or {}).get("market_quote_id")
    age = _age((cite or {}).get("captured_at"))
    if quote_id is None:
        if not args.allow_stated_only:
            sys.exit(f"no citable quote for {args.outcome_key}. The entry "
                     f"would be `stated_only` and counted nowhere. Pass "
                     f"--allow-stated-only to record it as documentation.")
    elif age is not None and age > QUOTE_MAX_AGE_S \
            and not args.allow_stated_only:
        sys.exit(f"the newest quote for {args.outcome_key} is {age}s old, "
                 f"past the {QUOTE_MAX_AGE_S}s ceiling — the server will "
                 f"refuse it as the price basis and the entry will count "
                 f"nowhere. Wait for the T-10 sweep, or pass "
                 f"--allow-stated-only.")
    body = {"fixture_id": fixture_id,
            "market_ticker": args.market_ticker or (cite or {}).get("ticker"),
            "outcome_key": args.outcome_key}
    if body["market_ticker"] is None:
        sys.exit("no market_ticker known for this outcome — pass "
                 "--market-ticker explicitly.")
    if quote_id is not None:
        body["market_quote_id"] = quote_id
    for k, v in (("stated_price", args.stated_price),
                 ("stated_size", args.stated_size),
                 ("rationale", args.rationale),
                 ("corrects_bet_id", args.corrects_bet_id)):
        if v is not None:
            body[k] = v
    if args.dry_run:
        print("DRY RUN — would POST /api/admin/mls/journal/view")
        print(json.dumps(body, indent=2))
        return 0
    out = _post("/api/admin/mls/journal/view", body)
    if out.get("error"):
        sys.exit(f"refused: {out['error']}")
    bet = out.get("bet") or out
    print(json.dumps(out, indent=2))
    basis = bet.get("price_basis")
    if basis != "observed_quote":
        print(f"\nWARNING: price_basis={basis!r}. This entry is recorded "
              f"honestly and counted NOWHERE.", file=sys.stderr)
    if bet.get("status") == "void":
        print("\nWARNING: status=void — recorded after kickoff, never "
              "counted.", file=sys.stderr)
    return 0


def cmd_resolve(args) -> int:
    out = _post("/api/admin/mls/journal/resolve",
                {"bet_id": args.bet_id, "status": args.status})
    if out.get("error"):
        sys.exit(f"refused: {out['error']}")
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Record a personal view against an MLS fixture.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="citable quote ids and the clock "
                                    "(no credential needed)")
    s.add_argument("event_id")
    s.set_defaults(fn=cmd_show)

    r = sub.add_parser("record", help="record a view at `considered`")
    r.add_argument("event_id")
    r.add_argument("outcome_key", choices=THREE_WAY)
    r.add_argument("--market-ticker")
    r.add_argument("--market-quote-id", type=int)
    r.add_argument("--stated-price")
    r.add_argument("--stated-size")
    r.add_argument("--rationale")
    r.add_argument("--corrects-bet-id", type=int)
    r.add_argument("--allow-stated-only", action="store_true",
                   help="record even though it will count nowhere")
    r.add_argument("--allow-void", action="store_true",
                   help="record after kickoff as documentation")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_record)

    v = sub.add_parser("resolve", help="considered -> taken | passed")
    v.add_argument("bet_id", type=int)
    v.add_argument("status", choices=("taken", "passed", "void"))
    v.set_defaults(fn=cmd_resolve)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
