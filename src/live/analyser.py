"""Repoint the existing live analyser at the MLS live plane.

The analyser itself is NOT rewritten here. `src/positions.py` already
prices hold-to-settlement against cash-out-now and alerts on the flip;
this module only supplies it with MLS rows instead of WC26 ones.

What changes is the *source*. The WC26 path is:

    load_schedule()          hardcoded tournament fixtures (archive plane)
      -> live_auto()         in-play re-simulation via SuggesterEngine
      -> rows_by_market      {market_id: {model/market prices}}
      -> evaluate_positions()

The MLS path assembled here is:

    Fixture (live plane)     competition_slug + status, provider identity
      -> MarketContract      the APPROVED Kalshi mapping, by ticker
      -> MarketQuote         newest captured book, integer cents
      -> PredictionRun       newest complete run's contract probabilities
      -> rows_by_market      the same shape
      -> evaluate_positions()

Two honesty constraints shape this module, and neither is optional.

1. **There is no in-play MLS model.** `src/live/model_mls.py` predicts a
   fixture from pre-match ratings only; nothing re-prices the remainder
   of a match in progress the way `SuggesterEngine.price_live` does for
   WC26. So every model probability this module can supply is PRE-MATCH.
   Rows are therefore stamped `model_basis="prematch"`, and once a
   fixture is in play `positions.evaluate_positions` refuses to compute a
   hold-side EV from it (verdict `STALE_MODEL`). Feeding a pre-match
   probability into an in-play hold-vs-exit comparison is not a
   conservative approximation — it is wrong in both directions: it
   suppresses the EXIT a losing position needs, and manufactures one for
   a winning position. Building an in-play MLS model is a modelling
   change owed an evaluation, not a repointing.

2. **The sell side never falls back to the ask.** `market_yes_bid` is
   derived with exactly the priority `src/kalshi_client._market_yes_bid`
   uses — yes bid, else 1 - no ask, and otherwise ABSENT. An absent bid
   means the exit is not executable (Jul 21 sell-side defect); the
   analyser must report NO_BID rather than quietly price an exit nobody
   can take.

This module reads. It writes nothing, places no order, and is unrelated
to `REAL_MONEY_SIGNALS_ENABLED` — pricing a position Son already holds
is not a trading path.
"""
from __future__ import annotations

import config
from src.live.models import (Fixture, MarketContract, MarketEvent,
                             MarketQuote, PredictionContract, PredictionRun,
                             Team)

# ntfy/Discord title for everything this module sends. The archive plane's
# WC26 alerts keep `send_alert`'s own default; an MLS alert must never
# arrive labelled WC26 (spec acceptance test 7). Kept short — ntfy shows
# the title in the notification header.
ALERT_TITLE = "MLS live"

# Fixture statuses ESPN reports, as stored by src/live/ingest.py.
STATUS_PRE, STATUS_IN, STATUS_POST = "pre", "in", "post"

# The model probability a live-plane run carries is pre-match by
# construction (see the module docstring). Named so callers cannot mistake
# it for a contemporaneous in-play read.
BASIS_PREMATCH = "prematch"


def _cents_to_prob(c: int | None) -> float | None:
    """Integer cents -> probability, or None. Rejects 0 and 100: a book
    quoted at the boundary is not a tradeable price."""
    if c is None:
        return None
    if not (0 < int(c) < 100):
        return None
    return round(int(c) / 100.0, 4)


def market_buy_probability(q: MarketQuote) -> float | None:
    """BUY-side implied probability — what a buyer actually pays.

    Mirrors `src/kalshi_client._market_yes_price`: the ASK first (a thin
    1c/2c book's midpoint is a multiplier nobody can trade), then the
    no-side bid, then the midpoint, then the last trade."""
    ask = _cents_to_prob(q.yes_ask_c)
    if ask is not None:
        return ask
    no_bid = _cents_to_prob(q.no_bid_c)
    if no_bid is not None:
        return round(1.0 - no_bid, 4)
    bid, no_ask = _cents_to_prob(q.yes_bid_c), _cents_to_prob(q.no_ask_c)
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 4)
    if no_bid is not None and no_ask is not None:
        return round(1.0 - (no_bid + no_ask) / 2.0, 4)
    return _cents_to_prob(q.last_trade_c)


def market_sell_probability(q: MarketQuote) -> float | None:
    """SELL-side price — what an exit actually realizes.

    Mirrors `src/kalshi_client._market_yes_bid`, INCLUDING its refusal to
    fall back to the ask or the midpoint. None means the exit is not
    executable, and `positions._verdict` turns that into NO_BID."""
    bid = _cents_to_prob(q.yes_bid_c)
    if bid is not None:
        return bid
    no_ask = _cents_to_prob(q.no_ask_c)
    if no_ask is not None:
        return round(1.0 - no_ask, 4)
    return None


def _newest_quotes(s, contract_ids: list[int]) -> dict[int, MarketQuote]:
    """Newest captured quote per market contract. One query, ordered so the
    last write per contract wins — capture batches share a captured_at, so
    id breaks the tie deterministically."""
    if not contract_ids:
        return {}
    rows = (s.query(MarketQuote)
            .filter(MarketQuote.market_contract_id.in_(contract_ids))
            .order_by(MarketQuote.captured_at.asc(), MarketQuote.id.asc())
            .all())
    return {q.market_contract_id: q for q in rows}


def _model_probabilities(s, fixture_id: int) -> tuple[dict[int, float], str | None]:
    """({market_contract_id: probability}, run_id) from this fixture's
    newest COMPLETE run.

    Deliberately the newest run rather than the canonical T-10 lock: the
    lock is frozen evidence for scoring, while a position read wants the
    freshest model available. Both are pre-match either way — see the
    module docstring — so this choice does not change the basis."""
    run = (s.query(PredictionRun)
           .filter_by(fixture_id=fixture_id, status="complete")
           .order_by(PredictionRun.captured_at.desc())
           .first())
    if run is None:
        return {}, None
    out: dict[int, float] = {}
    for c in (s.query(PredictionContract)
              .filter_by(prediction_run_id=run.id).all()):
        if c.market_contract_id is not None:
            out[c.market_contract_id] = c.raw_probability
    return out, run.id


def market_rows(s, fixture: Fixture) -> dict[str, dict]:
    """`rows_by_market` for one fixture, keyed by Kalshi ticker.

    The shape is exactly what `positions.evaluate_positions` consumes, so
    the analyser needs no adapter of its own. Only APPROVED market events
    contribute: an unapproved mapping is an unresolved identity claim, and
    pricing a position against a book we have not confirmed belongs to
    this fixture is the ambiguous-match failure AGENTS.md §6 forbids."""
    events = (s.query(MarketEvent)
              .filter_by(fixture_id=fixture.id, mapping_approved=True)
              .all())
    if not events:
        return {}
    contracts = (s.query(MarketContract)
                 .filter(MarketContract.market_event_id.in_(
                     [e.id for e in events]))
                 .all())
    quotes = _newest_quotes(s, [c.id for c in contracts])
    model_p, run_id = _model_probabilities(s, fixture.id)

    rows: dict[str, dict] = {}
    for c in contracts:
        q = quotes.get(c.id)
        if q is None:
            continue
        rows[c.ticker] = {
            "market_id": c.ticker,
            "market_title": c.side_label or c.ticker,
            "outcome_key": c.outcome_key,
            "market_probability": market_buy_probability(q),
            "market_yes_bid": market_sell_probability(q),
            # PRE-MATCH by construction. Never emitted under the
            # `live_model_probability` key — that key means an in-play
            # re-simulation, which the MLS plane does not have.
            "model_probability": model_p.get(c.id),
            "model_basis": BASIS_PREMATCH,
            "model_run_id": run_id,
            "quote_captured_at": q.captured_at,
        }
    return rows


def live_fixtures(s, statuses: tuple[str, ...] = (STATUS_PRE, STATUS_IN)):
    """Fixtures worth pricing right now, for the configured competition.

    `pre` is included on purpose: before kickoff the pre-match model IS
    contemporaneous, so the full hold-vs-exit verdict is honest there.
    Once a fixture is `in`, the same probability is stale and the verdict
    degrades to STALE_MODEL rather than lying."""
    return (s.query(Fixture)
            .filter(Fixture.competition_slug == config.COMPETITION,
                    Fixture.status.in_(statuses))
            .order_by(Fixture.current_kickoff_utc.asc())
            .all())


def _fixture_label(s, f: Fixture) -> str:
    home = s.get(Team, f.home_team_id) if f.home_team_id else None
    away = s.get(Team, f.away_team_id) if f.away_team_id else None
    if home is None or away is None:
        return f"fixture {f.espn_event_id}"
    return f"{home.canonical_name} vs {away.canonical_name}"


def evaluate_mls_positions(alert: bool = True) -> dict:
    """One analyser pass over every trackable MLS fixture.

    Returns counts with their denominators — `fixtures` is how many were
    considered, `priced` how many produced rows, `positions` how many open
    tracked positions were evaluated. A bare "3 positions" with no
    denominator is the reporting failure this repo has already paid for."""
    from src.live.db import get_session, plane_ready

    if not plane_ready():
        return {"dormant": True, "fixtures": 0, "priced": 0,
                "positions": 0, "reads": []}
    s = get_session()
    if s is None:
        return {"dormant": True, "fixtures": 0, "priced": 0,
                "positions": 0, "reads": []}
    try:
        fixtures = live_fixtures(s)
        priced = 0
        reads: list[dict] = []
        for f in fixtures:
            rows = market_rows(s, f)
            if not rows:
                continue
            priced += 1
            label = _fixture_label(s, f)
            in_play = f.status == STATUS_IN
            try:
                from src.positions import evaluate_positions
                reads.extend(evaluate_positions(
                    rows, str(f.espn_event_id), minute=None, alert=alert,
                    alert_title=ALERT_TITLE, alert_label=label,
                    in_play=in_play))
            except Exception as exc:   # one fixture must not kill the pass
                print(f"[mls-analyser] {f.espn_event_id} eval failed: {exc}")
        return {"dormant": False, "fixtures": len(fixtures),
                "priced": priced, "positions": len(reads), "reads": reads}
    finally:
        s.close()
