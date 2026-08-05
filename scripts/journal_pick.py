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


def cmd_record_viewer(args) -> int:
    """The viewer-competition write (#72): a fixture addressed by
    (competition_slug, provider_fixture_id) instead of an MLS espn id.

    No briefing exists for these competitions, so the operator supplies
    what the briefing would have: the kickoff and both club names. Two
    consequences the MLS path softens are structural here and the
    command says them up front rather than warning after:

      - every row is `stated_only`. There is no model and no approved
        market chain to grant `observed_quote` against, so there is no
        --allow-stated-only gate: choosing this subcommand IS the
        choice, and the row counts nowhere by construction.
      - the kickoff guard runs on the kickoff YOU state. Misstate it
        and the server still stamps `void` off its own copy on a
        repeat fixture — but a first write creates the fixture from
        your value, so the clock is only as honest as the input.
    """
    try:
        ko = datetime.fromisoformat(
            args.kickoff_utc.replace("Z", "+00:00"))
    except ValueError:
        sys.exit(f"--kickoff-utc {args.kickoff_utc!r} is not an ISO "
                 f"timestamp (e.g. 2026-08-05T23:30:00+00:00)")
    if ko.tzinfo is None:
        sys.exit("--kickoff-utc must carry a timezone offset — a naive "
                 "kickoff silently shifts the void rule")
    if _now() >= ko and not args.allow_void:
        sys.exit(f"kickoff was {(_now()-ko).total_seconds()/60:.0f} min "
                 f"ago. The server would record this as `void` — kept, "
                 f"counted nowhere. Pass --allow-void to record it "
                 f"anyway as documentation.")
    body = {"market_ticker": args.market_ticker,
            "outcome_key": args.outcome_key,
            "competition_slug": args.competition_slug,
            "provider_fixture_id": args.provider_fixture_id,
            "kickoff_utc": args.kickoff_utc,
            "home_team": args.home_team, "away_team": args.away_team}
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
    print(json.dumps(out, indent=2))
    print("\nNOTE: price_basis=stated_only by construction on a viewer "
          "competition — recorded honestly, counted NOWHERE.",
          file=sys.stderr)
    return 0


def cmd_execution(args) -> int:
    """Attach a REAL fill (or a real failure to fill) to a taken view.

    Every fill fact is operator-supplied; the server refuses invented
    ones, refuses fills that antedate the taken moment, and this
    command adds nothing to soften either refusal — the matchday-1
    slate's fills were correctly refused for exactly that chronology,
    and the fix is process (say taken before betting), not tooling.
    """
    body = {"bet_id": args.bet_id, "account_label": args.account_label,
            "consent_recorded_at": args.consent_recorded_at,
            "status": args.status}
    for k, v in (("fill_price", args.fill_price),
                 ("filled_contracts", args.filled_contracts),
                 ("fee_paid", args.fee_paid),
                 ("filled_at", args.filled_at),
                 ("market_quote_id_at_fill", args.market_quote_id_at_fill),
                 ("not_filled_reason", args.not_filled_reason),
                 ("best_available_price", args.best_available_price),
                 ("exchange_order_id", args.exchange_order_id)):
        if v is not None:
            body[k] = v
    if args.dry_run:
        print("DRY RUN — would POST /api/admin/mls/journal/execution")
        print(json.dumps(body, indent=2))
        return 0
    out = _post("/api/admin/mls/journal/execution", body)
    if out.get("error"):
        sys.exit(f"refused: {out['error']}")
    print(json.dumps(out, indent=2))
    return 0


def cmd_settlement(args) -> int:
    """The exchange's own settlement numbers — operator-supplied,
    never derived from a scoreboard."""
    body = {"execution_id": args.execution_id,
            "settlement_credit": args.settlement_credit,
            "settled_at": args.settled_at}
    if args.settled_outcome is not None:
        body["settled_outcome"] = args.settled_outcome
    if args.dry_run:
        print("DRY RUN — would POST /api/admin/mls/journal/settlement")
        print(json.dumps(body, indent=2))
        return 0
    out = _post("/api/admin/mls/journal/settlement", body)
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

    rv = sub.add_parser(
        "record-viewer",
        help="record a view on a viewer competition (leagues-cup etc.) "
             "— stated_only by construction, counts nowhere")
    rv.add_argument("outcome_key", choices=THREE_WAY)
    rv.add_argument("--competition-slug", required=True,
                    help="e.g. leagues-cup-2026 (season readable from "
                         "the slug)")
    rv.add_argument("--provider-fixture-id", required=True)
    rv.add_argument("--kickoff-utc", required=True,
                    help="ISO with offset, e.g. 2026-08-05T23:30:00+00:00")
    rv.add_argument("--home-team", required=True)
    rv.add_argument("--away-team", required=True)
    rv.add_argument("--market-ticker", required=True)
    rv.add_argument("--stated-price")
    rv.add_argument("--stated-size")
    rv.add_argument("--rationale")
    rv.add_argument("--corrects-bet-id", type=int)
    rv.add_argument("--allow-void", action="store_true",
                    help="record after kickoff as documentation")
    rv.add_argument("--dry-run", action="store_true")
    rv.set_defaults(fn=cmd_record_viewer)

    e = sub.add_parser("execution",
                       help="attach a real fill (or non-fill) to a "
                            "taken view — all facts operator-supplied")
    e.add_argument("bet_id", type=int)
    e.add_argument("account_label")
    e.add_argument("--consent-recorded-at", required=True,
                   help="when the operator's consent was recorded (ISO)")
    e.add_argument("--status", default="filled",
                   choices=("filled", "partial", "not_filled"))
    e.add_argument("--fill-price")
    e.add_argument("--filled-contracts")
    e.add_argument("--fee-paid")
    e.add_argument("--filled-at",
                   help="the ACTUAL fill moment (ISO) — required for "
                        "filled/partial; never approximated")
    e.add_argument("--market-quote-id-at-fill", type=int)
    e.add_argument("--not-filled-reason")
    e.add_argument("--best-available-price")
    e.add_argument("--exchange-order-id")
    e.add_argument("--dry-run", action="store_true")
    e.set_defaults(fn=cmd_execution)

    st = sub.add_parser("settlement",
                        help="the exchange's settlement numbers for an "
                             "execution")
    st.add_argument("execution_id", type=int)
    st.add_argument("settlement_credit")
    st.add_argument("--settled-at", required=True)
    st.add_argument("--settled-outcome")
    st.add_argument("--dry-run", action="store_true")
    st.set_defaults(fn=cmd_settlement)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
