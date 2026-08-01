"""Everything the platform knows about one fixture, at one instant.

A BRIEFING, NOT A RECOMMENDATION. There is deliberately no field naming
a side to back, no "edge", no rating of the opportunity. The operator
decides; this assembles what they decide from. No code path here places
an order, and none may.

THE ONE RULE THAT MAKES IT USABLE: every section either carries data or
says WHY it does not, in words. A blank and a measured absence must never
render alike — that confusion cost an hour on 2026-07-31, when two of
fifteen MLS fixtures served `books: []` and the payload could not say
whether the exchange listed nothing, the price had aged past the
freshness gate, or the lookup had raised.

WHAT IT REFUSES TO IMPLY. No model in this platform has an established
edge over the market:

  - MLS shadow approval is +0.0272, CI [-0.0071, +0.061] — crosses zero;
  - the friendlies read beats a coin flip narrowly (Brier 0.1892 vs
    0.1973) and the MARKET's own accuracy there is unmeasured;
  - the totals family scores 0.1696 against the book's 0.1691 — an echo.

So a difference between our number and the exchange's is a DISAGREEMENT.
The sheet says that in the payload, not just here.
"""
from __future__ import annotations

NOT_A_RECOMMENDATION = (
    "a briefing, not a recommendation. No side is named and no bet is "
    "implied. No model here has an established edge over the market, so a "
    "difference between our number and the exchange's is a DISAGREEMENT, "
    "not a mispricing")


def _absent(reason: str, means: str) -> dict:
    """A section with no data ALWAYS says why. `available: False` plus a
    named reason, never an empty object the caller must interpret."""
    return {"available": False, "reason": reason, "means": means}


def build(event_id: str, competition: str = "mls") -> dict:
    """Compose the sheet. Every section is independently fault-isolated:
    one dead provider must degrade its own section, never the sheet."""
    from datetime import datetime, timezone

    out: dict = {"event_id": event_id, "competition": competition,
                 "framing": NOT_A_RECOMMENDATION,
                 "generated_at": datetime.now(timezone.utc).isoformat()}

    # --- the fixture itself -------------------------------------------
    from src import mls
    match = None
    try:
        match = mls.match_summary(event_id)
    except Exception as exc:
        out["fixture"] = _absent(
            "summary_failed",
            f"the fixture feed raised ({type(exc).__name__}) — this is not "
            f"'no such fixture'")
    if match is None and "fixture" not in out:
        out["fixture"] = _absent(
            "summary_unavailable",
            "the fixture feed could not be read — not 'no such fixture'")
    if match:
        out["fixture"] = {"available": True, **match}

    home = (match or {}).get("home", {}).get("name") or ""
    away = (match or {}).get("away", {}).get("name") or ""
    kickoff = (match or {}).get("date")

    # --- the market ----------------------------------------------------
    books, meta = [], {}
    try:
        books, meta = mls.find_all_books_with_freshness(kickoff, home, away)
    except Exception as exc:
        meta = {"status": "unavailable",
                "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if books:
        out["market"] = {"available": True, "families": books,
                         "freshness": meta}
    else:
        out["market"] = _absent(
            f"book_{(meta or {}).get('status', 'unavailable')}",
            "no book is presented. `freshness.status` says which: the "
            "prices failed the freshness gate, no Kalshi event bridged "
            "this fixture, or the lookup raised — NOT that the exchange "
            "lists none")
        out["market"]["freshness"] = meta

    # --- the model -----------------------------------------------------
    try:
        from src.live import runs as live_runs
        model = live_runs.model_for_event(event_id)
    except Exception as exc:
        model = None
        out.setdefault("_errors", []).append(f"model: {exc}")
    if model:
        out["model"] = {"available": True, **model,
                        "means": ("shadow odds. An approval permits "
                                  "COLLECTING prospective evidence; it is "
                                  "not a claim of edge")}
    else:
        out["model"] = _absent(
            "no_run",
            "no prediction run exists for this fixture — the model "
            "declined or has not swept it yet, NOT a 50/50 read")

    # --- the market beside the model, on one scale ----------------------
    out["market_vs_model"] = _compare(out)

    # --- team news ------------------------------------------------------
    try:
        from src.live import team_news
        news = team_news.fixture_news(event_id)
        out["team_news"] = ({"available": True, **news} if news
                            else _absent("never_captured",
                                         "no lineup has been captured for "
                                         "this fixture — NOT 'no absences'"))
    except Exception as exc:
        out["team_news"] = _absent(
            "capture_failed",
            f"the team-news read raised ({type(exc).__name__}) — not "
            f"'no absences'")
    return out


def _compare(sheet: dict) -> dict:
    """Model against market on the SAME quantity, or a named refusal.

    Both sides must be a three-way probability partition. The comparison
    is arithmetic on two observed numbers — it is NOT evidence that
    either is right.
    """
    mk = sheet.get("market") or {}
    md = sheet.get("model") or {}
    if not mk.get("available"):
        return _absent("no_market", "nothing to compare against")
    if not md.get("available"):
        return _absent("no_model", "nothing to compare with")
    winner = next((f for f in mk.get("families") or []
                   if f.get("key") == "winner"), None)
    if not winner:
        return _absent("no_winner_family",
                       "the 3-way family is not listed, so the model's "
                       "3-way has no counterpart")
    legs = {}
    for row in winner.get("markets") or []:
        try:
            p = float(row.get("yes_ask"))
        except (TypeError, ValueError):
            continue
        if 0.0 < p < 1.0:
            legs[row.get("label") or ""] = p
    if len(legs) != 3:
        return _absent("book_not_three_way",
                       "fewer than three priced legs, so no partition "
                       "exists to remove the vig from")
    total = sum(legs.values())
    return {
        "available": True,
        "market_legs_devigged": {k: round(v / total, 4)
                                 for k, v in legs.items()},
        "market_vig": round(total - 1.0, 4),
        # The run's outcomes live at model.primary.outcomes. The first
        # version read md["outcomes"], which does not exist — and its
        # test PASSED because the test invented the payload shape instead
        # of using the real one, so the comparison rendered empty in
        # production on the single thing it exists to show.
        "model_outcomes": ((md.get("primary") or {}).get("outcomes")
                           or (md.get("latest") or {}).get("outcomes")
                           or {}),
        "model_run_id": (md.get("primary") or {}).get("run_id"),
        "means": ("two observed numbers side by side. A gap is a "
                  "DISAGREEMENT, not a verified mispricing — the market's "
                  "accuracy on this competition is the baseline our own "
                  "approval has never significantly beaten"),
    }
