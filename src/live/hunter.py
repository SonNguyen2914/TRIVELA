"""Kalshi soccer market hunter — an always-on OBSERVATIONAL scanner.

Scans Kalshi's soccer per-match ("...GAME") series for structurally
mispriced books and records findings. It never places an order, never
sizes one, and never issues advice: every output is an observation with
its full arithmetic attached. Money stays locked
(REAL_MONEY_SIGNALS_ENABLED=false) and nothing here reads that flag to
act — there is no order path.

Finding catalogue (each mechanically defined, net of EXACT fees):

  SUM_BELOW_ONE   a full mutually-exclusive 3-way game book whose YES
                  asks + per-leg fees sum under $1.00 (true structural
                  arbitrage). Guarded: exactly 3 legs, exactly one
                  TIE/DRAW leg, every leg priced.
  CROSSED_BOOK    one market whose complements violate no-arbitrage:
                  yes_bid + no_bid > $1 + fees on both taker legs.
  POST_CERTAINTY  ESPN reports the match finished (state 'post', outcome
                  derived from the per-side score NUMBERS — never the
                  winner-first composite string) while the mapped Kalshi
                  market still offers the certain outcome below
                  $1 - fee. ESPN is re-read at detection time, never
                  cached from a previous cycle. MLS only today — the
                  only competition with an approved fixture mapping.
  WIDE_SPREAD /   liquidity CONTEXT flags (config thresholds). Labelled
  THIN_BOOK       is_context=True; they are never wins and never alert.
  MODEL_EDGE      readout ONLY where an approved model exists (today:
                  MLS). Reuses the existing machinery — probabilities
                  come from the stored PredictionRun/PredictionContract
                  rows and the fee from the exact fee module — and every
                  row carries the standing approval qualifier verbatim.
                  Competitions without an approval get "no model",
                  never a number.
  IN_PLAY_        market-anchored repricing: the same market's mid moved
  OVERREACTION    more than a config threshold between two consecutive
                  hunter captures (capture-PAIRED: both of OUR clocks
                  stored) on a match dated today, AND the move exceeds
                  the wider of the two captures' spreads (spread-aware —
                  a "move" inside quote noise is not a repricing).
                  CONDITIONALITY DISCIPLINE: a violent in-play move is
                  usually conditioned on a real match event (goal, red
                  card) this scanner cannot observe across arbitrary
                  competitions, so the row records the conditioning
                  event as UNOBSERVED, is labelled context, never claims
                  mispricing, and never alerts. The pair store is
                  process-local: the first cycle after a restart has no
                  pair and detects nothing, recorded honestly.

Provider facts this module is built on (verified, archived in
research_archive/kalshi_soccer_taxonomy_2026-07-28.json):
  - market payloads carry *_dollars price strings (exact Decimal) and
    *_fp size strings; integer-cent fields may be absent entirely;
  - Kalshi publishes NO quote timestamp (updated_time is the market
    DEFINITION clock, ~30h stale on active books) — every recorded
    price carries OUR capture clock and all freshness logic uses it;
  - Kalshi rate-limits hard: one throttled request stream, cursor
    pagination with a page cap, 429 backoff honouring Retry-After.

Alert discipline: only SUM_BELOW_ONE / CROSSED_BOOK / POST_CERTAINTY
above a config margin may alert, through the rule-based src.alerts path
(never the journal broadcast). Copy is observational arithmetic — no
imperatives, no "BUY"/"TAKE" — always with the shadow-mode framing, and
rate-limited to HUNTER_ALERT_MAX_PER_HOUR.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

import config
from src.live.db import get_session, plane_ready
from src.live.models import HunterCycle, HunterFinding
from src.live.paper import FEE_POLICY, order_fee_dollars

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAME_SERIES_SUFFIX = "GAME"
SOCCER_TAG = "Soccer"
MLS_SERIES = "KXMLSGAME"

ALERTABLE = ("SUM_BELOW_ONE", "CROSSED_BOOK", "POST_CERTAINTY")
CONTEXT_TYPES = ("WIDE_SPREAD", "THIN_BOOK", "IN_PLAY_OVERREACTION")

# Findings never claim more than this framing allows. Attached to every
# alert and to the API report.
SHADOW_FRAMING = ("shadow mode — observational record only; no order was "
                  "or will be placed, and nothing here is advice")

CAPTURE_CLOCK_NOTE = ("all capture timestamps are OUR clock; Kalshi "
                      "publishes no quote timestamp (updated_time is the "
                      "definition clock)")

# Hard bounds so the scan can never fan out without limit.
MAX_SERIES_PER_CYCLE = 120
MAX_PAGES_PER_SERIES = 3
PAGE_LIMIT = 200

ONE = Decimal("1")

# --- throttled provider access --------------------------------------------
_MIN_GAP_S = 0.35
_last_call = 0.0


def _now():
    return datetime.now(timezone.utc)


def _get(url: str, params: dict | None = None, max_retries: int = 4) -> dict:
    """Throttled GET with 429 backoff (Retry-After honoured). One shared
    pacing clock for every hunter request — the repo has been burned by
    Kalshi rate limits before."""
    global _last_call
    delay = 2.0
    for _ in range(max_retries):
        wait = _MIN_GAP_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay = min(delay * 2, 15.0)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def _paged_markets(series: str, counter: dict) -> list[dict]:
    """Open markets for one series, cursor-paged with a hard cap."""
    out: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES_PER_SERIES):
        params = {"series_ticker": series, "status": "open",
                  "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        d = _get(f"{KALSHI}/markets", params)
        counter["requests"] = counter.get("requests", 0) + 1
        out.extend(d.get("markets") or [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


# --- exact price/size parsing ---------------------------------------------
def _price(m: dict, field: str) -> Decimal | None:
    """Exact price in dollars for yes_bid/yes_ask/no_bid/no_ask: the
    provider *_dollars string first (current schema), integer cents as
    fallback. Only a real two-sided-book value in (0, 1) counts — 0 and
    1 are empty-side placeholders."""
    v = m.get(f"{field}_dollars")
    if v is not None:
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError):
            return None
    else:
        c = m.get(field)
        if not isinstance(c, int):
            return None
        d = Decimal(c) / 100
    if d <= 0 or d >= 1:
        return None
    return d


def _size(m: dict, field: str) -> Decimal | None:
    v = m.get(f"{field}_fp")
    if v is None:
        v = m.get(field)
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fee(price: Decimal) -> Decimal:
    """Exact Kalshi general-taker fee for ONE contract at `price` —
    the repo's Decimal fee module, never a float approximation."""
    return order_fee_dollars(price, 1)


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


# --- roster discovery ------------------------------------------------------
# Module-level cache: refreshed every HUNTER_DISCOVERY_MINUTES. Between
# discoveries only series that recently had open markets are scanned, so
# the steady-state request budget stays proportional to live soccer, not
# to the whole taxonomy.
_roster: dict = {"at": None, "series": [], "active": set()}


def discover_roster(counter: dict) -> list[str] | None:
    """The soccer per-match series roster, discovered empirically from
    the provider's series listing (tag Soccer, ticker ends GAME) minus
    the config skip-list. Config HUNTER_SERIES overrides discovery
    entirely. Returns None on failure so a stale roster is kept rather
    than silently scanning nothing."""
    if config.HUNTER_SERIES:
        return list(config.HUNTER_SERIES)
    try:
        d = _get(f"{KALSHI}/series", {"category": "Sports"})
        counter["requests"] = counter.get("requests", 0) + 1
    except requests.RequestException as exc:
        print(f"[hunter] roster discovery failed: {exc}")
        return None
    skip = set(config.HUNTER_SERIES_SKIP)
    out = sorted({s.get("ticker") for s in (d.get("series") or [])
                  if s.get("ticker")
                  and s["ticker"].endswith(GAME_SERIES_SUFFIX)
                  and SOCCER_TAG in (s.get("tags") or [])
                  and s["ticker"] not in skip})
    return out or None


# --- detectors -------------------------------------------------------------
def _tie_leg(ticker: str) -> bool:
    return (ticker or "").rsplit("-", 1)[-1].upper() in ("TIE", "DRAW")


def detect_event_findings(series: str, event_ticker: str,
                          markets: list[dict], captured_at: datetime
                          ) -> list[dict]:
    """SUM_BELOW_ONE for one event plus the per-market detectors
    (CROSSED_BOOK and the liquidity context flags). Pure: in-memory
    market dicts in, finding dicts out."""
    found: list[dict] = []
    cap = captured_at.isoformat()

    # SUM_BELOW_ONE — only on a provable full partition: exactly three
    # legs, exactly one TIE/DRAW leg, every leg with a real ask.
    ties = [m for m in markets if _tie_leg(m.get("ticker", ""))]
    if len(markets) == 3 and len(ties) == 1:
        asks = [(m, _price(m, "yes_ask")) for m in markets]
        if all(a is not None for _, a in asks):
            total_ask = sum(a for _, a in asks)
            fees = [(m, a, _fee(a)) for m, a in asks]
            total_fee = sum(f for _, _, f in fees)
            margin = ONE - total_ask - total_fee
            if margin > 0:
                found.append({
                    "finding_type": "SUM_BELOW_ONE",
                    "series": series, "event_ticker": event_ticker,
                    "market_ticker": None, "is_context": False,
                    "net_margin_dollars": str(margin),
                    "legs": {
                        "rule": "1 - sum(yes_ask) - sum(fee_per_leg) > 0 "
                                "over a full 3-way partition",
                        "captured_at": cap,
                        "legs": [{"ticker": m.get("ticker"),
                                  "yes_ask_dollars": _s(a),
                                  "fee_dollars": _s(f)}
                                 for m, a, f in fees],
                        "sum_asks_dollars": str(total_ask),
                        "sum_fees_dollars": str(total_fee),
                        "payout_dollars": "1",
                        "net_margin_dollars": str(margin),
                        "fee_policy": FEE_POLICY["version"],
                    }})

    for m in markets:
        tick = m.get("ticker")
        yes_bid = _price(m, "yes_bid")
        yes_ask = _price(m, "yes_ask")
        no_bid = _price(m, "no_bid")

        # CROSSED_BOOK — selling both sides above $1: executing it means
        # buying NO at (1 - yes_bid) and YES at (1 - no_bid) as a taker,
        # so both fee legs are charged at those prices.
        if yes_bid is not None and no_bid is not None:
            gross = yes_bid + no_bid - ONE
            if gross > 0:
                fee_a = _fee(ONE - yes_bid)
                fee_b = _fee(ONE - no_bid)
                margin = gross - fee_a - fee_b
                if margin > 0:
                    found.append({
                        "finding_type": "CROSSED_BOOK",
                        "series": series, "event_ticker": event_ticker,
                        "market_ticker": tick, "is_context": False,
                        "net_margin_dollars": str(margin),
                        "legs": {
                            "rule": "yes_bid + no_bid - 1 - fee(1-yes_bid)"
                                    " - fee(1-no_bid) > 0",
                            "captured_at": cap,
                            "yes_bid_dollars": _s(yes_bid),
                            "no_bid_dollars": _s(no_bid),
                            "fee_leg_yes_dollars": str(fee_a),
                            "fee_leg_no_dollars": str(fee_b),
                            "net_margin_dollars": str(margin),
                            "fee_policy": FEE_POLICY["version"],
                        }})

        # Liquidity CONTEXT flags — observational, never wins, never
        # alerted. Labelled as such in the row itself.
        wide = Decimal(str(config.HUNTER_WIDE_SPREAD_DOLLARS))
        if yes_bid is not None and yes_ask is not None \
                and yes_ask - yes_bid >= wide:
            found.append({
                "finding_type": "WIDE_SPREAD",
                "series": series, "event_ticker": event_ticker,
                "market_ticker": tick, "is_context": True,
                "net_margin_dollars": None,
                "legs": {"rule": f"yes_ask - yes_bid >= {wide} (context "
                                 "flag, not a win)",
                         "captured_at": cap,
                         "yes_bid_dollars": _s(yes_bid),
                         "yes_ask_dollars": _s(yes_ask),
                         "spread_dollars": str(yes_ask - yes_bid)}})

        ask_size = _size(m, "yes_ask_size")
        bid_size = _size(m, "yes_bid_size")
        min_size = Decimal(config.HUNTER_THIN_BOOK_SIZE)
        thin_reasons = []
        if yes_ask is None or yes_bid is None:
            thin_reasons.append("one_sided_or_empty_book")
        else:
            for side, sz in (("yes_ask_size", ask_size),
                             ("yes_bid_size", bid_size)):
                if sz is not None and sz < min_size:
                    thin_reasons.append(f"{side}_below_{min_size}")
        if thin_reasons:
            found.append({
                "finding_type": "THIN_BOOK",
                "series": series, "event_ticker": event_ticker,
                "market_ticker": tick, "is_context": True,
                "net_margin_dollars": None,
                "legs": {"rule": "top-of-book size below threshold or "
                                 "one-sided book (context flag, not a win)",
                         "captured_at": cap,
                         "reasons": thin_reasons,
                         "yes_bid_dollars": _s(yes_bid),
                         "yes_ask_dollars": _s(yes_ask),
                         "yes_bid_size": _s(bid_size),
                         "yes_ask_size": _s(ask_size)}})
    return found


# --- IN_PLAY_OVERREACTION --------------------------------------------------
# Previous-cycle top-of-book per ticker, in memory. Process-local BY
# DESIGN: persisting every quote is the volume-growth pattern that
# filled the production disk on Jul 25 (payloads with no reader). The
# cost of the choice is one blind cycle after a restart, and the
# finding's arithmetic is persisted in full when one fires.
_pair_store: dict[str, dict] = {}

_TICKER_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})")


def _event_dated_today(event_ticker: str, at: datetime) -> bool:
    """Kalshi ticker dates are US-Eastern wall clock (same rule the MLS
    mapper uses). This is a PROXY for in-play — kickoff time is unknown
    for unmapped competitions — and the finding says so explicitly."""
    m = _TICKER_DATE_RE.search(event_ticker or "")
    if not m:
        return False
    from zoneinfo import ZoneInfo
    d = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    today = (d.astimezone(ZoneInfo("America/New_York"))
             .strftime("%y%b%d").upper())
    return m.group(1) == today


def detect_overreaction(series: str, event_ticker: str, m: dict,
                        prev: dict | None, captured_at: datetime
                        ) -> dict | None:
    """Market-anchored repricing between two consecutive hunter captures
    of the SAME market — the market compared to itself, no model
    anywhere. Fires only when, on a match dated today (ET):

      |mid_now - mid_prev| >= HUNTER_OVERREACTION_MIN_MOVE_DOLLARS
      AND |mid_now - mid_prev| > max(spread_prev, spread_now)

    Both captures must be two-sided; both capture timestamps (OUR
    clocks) are stored — capture-paired. CONDITIONALITY: the move may be
    a correct instant repricing of an unobserved match event, so the
    finding is context, records that the conditioning event is
    unobserved, and never alerts."""
    if prev is None or not _event_dated_today(event_ticker, captured_at):
        return None
    yes_bid = _price(m, "yes_bid")
    yes_ask = _price(m, "yes_ask")
    if yes_bid is None or yes_ask is None or yes_ask < yes_bid:
        return None
    try:
        prev_bid = Decimal(prev["yes_bid"])
        prev_ask = Decimal(prev["yes_ask"])
    except (KeyError, InvalidOperation, TypeError):
        return None
    if prev_ask < prev_bid:
        return None
    mid_now = (yes_bid + yes_ask) / 2
    mid_prev = (prev_bid + prev_ask) / 2
    delta = abs(mid_now - mid_prev)
    spread_now = yes_ask - yes_bid
    spread_prev = prev_ask - prev_bid
    min_move = Decimal(str(config.HUNTER_OVERREACTION_MIN_MOVE_DOLLARS))
    if delta < min_move or delta <= max(spread_now, spread_prev):
        return None
    return {
        "finding_type": "IN_PLAY_OVERREACTION",
        "series": series, "event_ticker": event_ticker,
        "market_ticker": m.get("ticker"), "is_context": True,
        "net_margin_dollars": None,
        "legs": {
            "rule": (f"|mid_now - mid_prev| >= {min_move} AND > "
                     "max(spread_prev, spread_now), event dated today "
                     "(context flag, not a win)"),
            "capture_pair": {
                "prev": {"captured_at": prev.get("captured_at"),
                         "yes_bid_dollars": str(prev_bid),
                         "yes_ask_dollars": str(prev_ask),
                         "mid_dollars": str(mid_prev),
                         "spread_dollars": str(spread_prev)},
                "now": {"captured_at": captured_at.isoformat(),
                        "yes_bid_dollars": str(yes_bid),
                        "yes_ask_dollars": str(yes_ask),
                        "mid_dollars": str(mid_now),
                        "spread_dollars": str(spread_now)},
            },
            "mid_move_dollars": str(delta),
            "in_play_basis": ("ticker date segment matches today "
                              "US/Eastern; kickoff and live state are "
                              "UNVERIFIED for unmapped competitions"),
            "conditionality": ("the conditioning match event (goal, red "
                               "card, abandonment...) is UNOBSERVED by "
                               "this scanner; a large move may be a "
                               "CORRECT instant repricing — this row "
                               "claims a move happened, never that the "
                               "market is wrong"),
        }}


def _update_pair_store(m: dict, captured_at: datetime) -> None:
    tick = m.get("ticker")
    if not tick:
        return
    yes_bid = _price(m, "yes_bid")
    yes_ask = _price(m, "yes_ask")
    if yes_bid is None or yes_ask is None:
        _pair_store.pop(tick, None)      # a one-sided book breaks the pair
        return
    _pair_store[tick] = {"yes_bid": str(yes_bid), "yes_ask": str(yes_ask),
                         "captured_at": captured_at.isoformat()}


# ESPN re-read for POST_CERTAINTY. Separate function so tests count the
# calls: detection must hit the provider fresh, never a cached state.
def _fetch_espn_scoreboard(date_yyyymmdd: str) -> dict:
    r = requests.get(
        "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/"
        "scoreboard", params={"dates": date_yyyymmdd}, timeout=15)
    r.raise_for_status()
    return r.json()


def _fixture_local_date(dt: datetime) -> str:
    from zoneinfo import ZoneInfo
    d = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return d.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def detect_post_certainty(s, markets_by_ticker: dict[str, dict],
                          captured_at: datetime) -> list[dict]:
    """Matches ESPN says are FINISHED whose mapped Kalshi market still
    trades the certain outcome below $1 - fee.

    MLS only: it is the one competition with an approved fixture mapping.
    The ESPN state is fetched HERE, at detection time — never reused from
    an earlier cycle — and the outcome is derived from the per-side score
    NUMBERS (ingest._event_to_fields), never a composite string. Both
    observations carry capture timestamps."""
    from src.live.ingest import _event_to_fields
    from src.live.models import Fixture, MarketContract, MarketEvent

    found: list[dict] = []
    horizon = captured_at - timedelta(minutes=100)
    events = (s.query(MarketEvent)
              .filter_by(series=MLS_SERIES, mapping_approved=True)
              .filter(MarketEvent.fixture_id.isnot(None)).all())
    candidates = []
    for me in events:
        fx = s.get(Fixture, me.fixture_id)
        if fx is None or fx.current_kickoff_utc is None \
                or not fx.espn_event_id:
            continue
        ko = fx.current_kickoff_utc
        ko = ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)
        if ko <= horizon:
            # only events whose contracts are actually trading now
            tickers = [c.ticker for c in s.query(MarketContract)
                       .filter_by(market_event_id=me.id).all()
                       if c.ticker in markets_by_ticker]
            if tickers:
                candidates.append((me, fx, ko))
    if not candidates:
        return found

    by_date: dict[str, list] = {}
    for me, fx, ko in candidates:
        by_date.setdefault(_fixture_local_date(ko), []).append((me, fx))

    for date_str, items in by_date.items():
        try:
            payload = _fetch_espn_scoreboard(date_str)
        except requests.RequestException as exc:
            print(f"[hunter] espn re-read {date_str} failed: {exc}")
            continue
        espn_at = _now()
        espn_events = {str(ev.get("id")): ev
                       for ev in (payload.get("events") or [])}
        for me, fx in items:
            ev = espn_events.get(str(fx.espn_event_id))
            if not ev:
                continue
            f = _event_to_fields(ev)
            if not f or f.get("status") != "post":
                continue
            hg, ag = f.get("home_goals"), f.get("away_goals")
            if hg is None or ag is None:
                continue
            # outcome derived from the NUMBERS beside each side — the
            # ESPN composite `score` string is winner-first and must
            # never be parsed (schema-drift incident, Jul 24)
            outcome = ("home_win" if hg > ag
                       else "away_win" if ag > hg else "draw")
            from src.live.models import MarketContract as MC
            mc = (s.query(MC).filter_by(market_event_id=me.id,
                                        outcome_key=outcome).first())
            if mc is None or mc.ticker not in markets_by_ticker:
                continue
            m = markets_by_ticker[mc.ticker]
            ask = _price(m, "yes_ask")
            if ask is None:
                continue
            fee = _fee(ask)
            margin = ONE - ask - fee
            if margin > 0:
                found.append({
                    "finding_type": "POST_CERTAINTY",
                    "series": MLS_SERIES,
                    "competition_slug": "mls-2026",
                    "event_ticker": me.kalshi_event_ticker,
                    "market_ticker": mc.ticker,
                    "fixture_id": fx.id,
                    "is_context": False,
                    "net_margin_dollars": str(margin),
                    "espn_captured_at": espn_at,
                    "legs": {
                        "rule": "ESPN state 'post' at detection-time "
                                "re-read; certain outcome still asked "
                                "below 1 - fee",
                        "kalshi_captured_at": captured_at.isoformat(),
                        "espn_captured_at": espn_at.isoformat(),
                        "espn_event_id": fx.espn_event_id,
                        "final_score_home": hg, "final_score_away": ag,
                        "certain_outcome": outcome,
                        "yes_ask_dollars": _s(ask),
                        "fee_dollars": str(fee),
                        "net_margin_dollars": str(margin),
                        "fee_policy": FEE_POLICY["version"],
                    }})
    return found


def _model_qualifier() -> dict | None:
    """The standing approval qualifier for the MLS model, read from the
    ACTIVE persisted approval decision — never recomputed, never
    hardcoded. None when no approval exists ("no model")."""
    try:
        from src.live import model_eval
        dec = model_eval.current_approval_decision()
    except Exception as exc:
        print(f"[hunter] approval read failed: {exc}")
        return None
    if not dec or dec.get("approval_decision_missing"):
        return None
    return {
        "edge_vs_baseline": dec.get("edge_vs_baseline"),
        "ci_low": dec.get("ci_low"), "ci_high": dec.get("ci_high"),
        "n_scored": dec.get("n_scored"),
        "significant": bool(dec.get("edge_significant")),
        "decision_id": dec.get("decision_id"),
        "decision_content_hash": dec.get("content_hash"),
        "note": "point estimate only; the interval includes zero unless "
                "'significant' is true — never read this as an "
                "established edge",
    }


def detect_model_edge(s, markets_by_ticker: dict[str, dict],
                      captured_at: datetime) -> list[dict]:
    """MODEL_EDGE readouts for mapped, upcoming MLS 3-way contracts.

    ONLY where an approved model exists (the active approval decision);
    with none, this returns nothing and the report says "no model". The
    probabilities are the stored output of the existing machinery
    (PredictionRun / PredictionContract) and the fee is the exact fee
    module — nothing is re-modelled here. Observational; never alerts."""
    from src.live.models import (Fixture, MarketContract, MarketEvent,
                                 PredictionContract, PredictionRun)
    qualifier = _model_qualifier()
    if qualifier is None:
        return []
    found: list[dict] = []
    events = (s.query(MarketEvent)
              .filter_by(series=MLS_SERIES, mapping_approved=True)
              .filter(MarketEvent.fixture_id.isnot(None)).all())
    for me in events:
        fx = s.get(Fixture, me.fixture_id)
        if fx is None or fx.current_kickoff_utc is None:
            continue
        ko = fx.current_kickoff_utc
        ko = ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)
        if ko <= captured_at:            # upcoming fixtures only
            continue
        run = (s.query(PredictionRun)
               .filter_by(fixture_id=fx.id, status="complete")
               .order_by(PredictionRun.created_at.desc()).first())
        if run is None:
            continue
        contracts = {c.ticker: c for c in
                     s.query(MarketContract)
                     .filter_by(market_event_id=me.id).all()}
        for pc in (s.query(PredictionContract)
                   .filter_by(prediction_run_id=run.id).all()):
            if pc.outcome_key not in ("home_win", "draw", "away_win"):
                continue
            mc = next((c for c in contracts.values()
                       if c.outcome_key == pc.outcome_key), None)
            if mc is None or mc.ticker not in markets_by_ticker:
                continue
            m = markets_by_ticker[mc.ticker]
            ask = _price(m, "yes_ask")
            if ask is None:
                continue
            p = (pc.anchored_probability
                 if pc.anchored_probability is not None
                 else pc.raw_probability)
            if p is None:
                continue
            fee = _fee(ask)
            net_edge = Decimal(str(p)) - ask - fee
            if net_edge < Decimal(str(config.HUNTER_MODEL_EDGE_MIN)):
                continue
            found.append({
                "finding_type": "MODEL_EDGE",
                "series": MLS_SERIES, "competition_slug": "mls-2026",
                "event_ticker": me.kalshi_event_ticker,
                "market_ticker": mc.ticker, "fixture_id": fx.id,
                "is_context": False,
                "net_margin_dollars": str(net_edge),
                "model_qualifier": qualifier,
                "legs": {
                    "rule": "model_p - yes_ask - fee >= "
                            f"{config.HUNTER_MODEL_EDGE_MIN} (readout of "
                            "the existing shadow model; observational)",
                    "captured_at": captured_at.isoformat(),
                    "outcome_key": pc.outcome_key,
                    "model_probability": p,
                    "prediction_run_id": run.id,
                    "yes_ask_dollars": _s(ask),
                    "fee_dollars": str(fee),
                    "net_edge_dollars": str(net_edge),
                    "fee_policy": FEE_POLICY["version"],
                    "standing_qualifier": qualifier,
                }})
    return found


# --- alerts ----------------------------------------------------------------
_alert_times: deque = deque()


def _alert_budget_ok(now: datetime) -> bool:
    cutoff = now - timedelta(hours=1)
    while _alert_times and _alert_times[0] < cutoff:
        _alert_times.popleft()
    return len(_alert_times) < config.HUNTER_ALERT_MAX_PER_HOUR


def _maybe_alert(f: dict, now: datetime) -> bool:
    """Observational alert for a NEW structural finding above the config
    margin. Rule-based path (src.alerts), never the journal broadcast.
    Copy states arithmetic only — no imperative, no recommendation."""
    if f["finding_type"] not in ALERTABLE or f.get("is_context"):
        return False
    margin = Decimal(f["net_margin_dollars"])
    if margin < Decimal(str(config.HUNTER_ALERT_MIN_MARGIN_DOLLARS)):
        return False
    if not _alert_budget_ok(now):
        print(f"[hunter] alert suppressed (budget): {f['finding_type']} "
              f"{f.get('market_ticker') or f.get('event_ticker')}")
        return False
    subject = f.get("market_ticker") or f.get("event_ticker") or f["series"]
    legs = f.get("legs") or {}
    if f["finding_type"] == "SUM_BELOW_ONE":
        detail = (f"3-way yes asks sum ${legs.get('sum_asks_dollars')}, "
                  f"fees ${legs.get('sum_fees_dollars')}")
    elif f["finding_type"] == "CROSSED_BOOK":
        detail = (f"yes_bid ${legs.get('yes_bid_dollars')} + no_bid "
                  f"${legs.get('no_bid_dollars')} exceeds $1 plus fees")
    else:
        detail = (f"ESPN final {legs.get('final_score_home')}-"
                  f"{legs.get('final_score_away')} "
                  f"({legs.get('certain_outcome')}); still asked at "
                  f"${legs.get('yes_ask_dollars')}")
    msg = (f"🔎 hunter observation [{f['finding_type']}] {subject}: "
           f"{detail}; net margin ${margin} per contract after exact "
           f"fees ({FEE_POLICY['version']}). "
           f"({SHADOW_FRAMING}.)")
    try:
        from src.alerts import send_alert
        send_alert(msg, title="Trivela hunter")
        _alert_times.append(now)
        return True
    except Exception as exc:              # alerting must never break a scan
        print(f"[hunter] alert failed: {exc}")
        return False


# --- persistence + the cycle ----------------------------------------------
def _finding_key(f: dict) -> str:
    return f"{f['finding_type']}|{f.get('market_ticker') or f.get('event_ticker')}"


def _row_key(row: HunterFinding) -> str:
    return f"{row.finding_type}|{row.market_ticker or row.event_ticker}"


def scan_cycle() -> dict:
    """One full hunter pass: roster upkeep, per-series market fetch,
    detection, append-only persistence with expiry, alerts, and the
    heartbeat/denominator cycle row. Registered in the scheduler with
    coalesce + max_instances=1."""
    if not config.HUNTER_ENABLED:
        return {"skipped": "disabled"}
    if not plane_ready():
        return {"skipped": "dormant"}
    started = _now()
    counter: dict = {"requests": 0}

    # roster upkeep (module cache; discovery every HUNTER_DISCOVERY_MINUTES)
    discovery_due = (
        _roster["at"] is None
        or (started - _roster["at"]).total_seconds()
        >= config.HUNTER_DISCOVERY_MINUTES * 60)
    if discovery_due:
        roster = discover_roster(counter)
        if roster is not None:
            _roster["series"] = roster
            _roster["at"] = started
    series_all = list(_roster["series"])
    # between discoveries, only series that recently had open markets
    if discovery_due or not _roster["active"]:
        to_scan = series_all
    else:
        to_scan = [t for t in series_all if t in _roster["active"]]
    to_scan = to_scan[:MAX_SERIES_PER_CYCLE]

    events_seen = markets_seen = 0
    new_findings: list[dict] = []
    scanned_series: set[str] = set()
    mls_markets_by_ticker: dict[str, dict] = {}
    active_now: set[str] = set()
    error: str | None = None

    try:
        for series in to_scan:
            try:
                ms = _paged_markets(series, counter)
            except requests.RequestException as exc:
                print(f"[hunter] {series} fetch failed: {exc}")
                continue
            captured_at = _now()
            scanned_series.add(series)
            if ms:
                active_now.add(series)
            markets_seen += len(ms)
            by_event: dict[str, list[dict]] = {}
            for m in ms:
                by_event.setdefault(m.get("event_ticker") or "", []).append(m)
                if series == MLS_SERIES and m.get("ticker"):
                    mls_markets_by_ticker[m["ticker"]] = m
            events_seen += len(by_event)
            for ev_ticker, ev_markets in by_event.items():
                new_findings.extend(detect_event_findings(
                    series, ev_ticker, ev_markets, captured_at))
                # capture-paired repricing vs the PREVIOUS cycle's book,
                # then refresh the pair store for the next cycle
                for m in ev_markets:
                    f = detect_overreaction(
                        series, ev_ticker, m,
                        _pair_store.get(m.get("ticker")), captured_at)
                    if f:
                        new_findings.append(f)
                    _update_pair_store(m, captured_at)
    except Exception as exc:               # provider loop must not kill DB write
        error = f"scan: {exc}"
        print(f"[hunter] scan error: {exc}")

    _roster["active"] = active_now if scanned_series else _roster["active"]

    s = get_session()
    created = expired = alerted = 0
    try:
        mls_capture = _now()
        if MLS_SERIES in scanned_series:
            try:
                new_findings.extend(detect_post_certainty(
                    s, mls_markets_by_ticker, mls_capture))
            except Exception as exc:
                print(f"[hunter] post-certainty error: {exc}")
            try:
                new_findings.extend(detect_model_edge(
                    s, mls_markets_by_ticker, mls_capture))
            except Exception as exc:
                print(f"[hunter] model-edge error: {exc}")

        now = _now()
        current_keys = {_finding_key(f) for f in new_findings}
        open_rows = (s.query(HunterFinding)
                     .filter_by(status="open").all())
        open_by_key = {}
        for row in open_rows:
            open_by_key[_row_key(row)] = row
            # expire an open finding whose series WAS scanned this cycle
            # but whose condition no longer holds. Never delete.
            if row.series in scanned_series \
                    and _row_key(row) not in current_keys:
                row.status = "expired"
                row.expired_at = now
                expired += 1
        for f in new_findings:
            key = _finding_key(f)
            existing = open_by_key.get(key)
            if existing is not None and existing.status == "open":
                existing.last_seen_at = now
                existing.observed_cycles = (existing.observed_cycles or 1) + 1
                continue
            row = HunterFinding(
                competition_slug=f.get("competition_slug"),
                series=f["series"],
                event_ticker=f.get("event_ticker"),
                market_ticker=f.get("market_ticker"),
                finding_type=f["finding_type"],
                is_context=bool(f.get("is_context")),
                legs_json=json.dumps(f["legs"], sort_keys=True),
                net_margin_dollars=f.get("net_margin_dollars"),
                fee_policy_version=(FEE_POLICY["version"]
                                    if not f.get("is_context") else None),
                model_qualifier_json=(
                    json.dumps(f["model_qualifier"], sort_keys=True)
                    if f.get("model_qualifier") else None),
                fixture_id=f.get("fixture_id"),
                first_captured_at=now, last_seen_at=now,
                observed_cycles=1,
                espn_captured_at=f.get("espn_captured_at"),
                status="open")
            if _maybe_alert(f, now):
                row.alerted_at = now
                alerted += 1
            s.add(row)
            open_by_key[key] = row
            created += 1

        s.add(HunterCycle(
            started_at=started, completed_at=_now(),
            status="failed" if error else "complete",
            series_scanned=len(scanned_series),
            events_scanned=events_seen, markets_scanned=markets_seen,
            findings_new=created, findings_expired=expired,
            request_count=counter.get("requests", 0),
            roster_size=len(series_all), active_series=len(active_now),
            error=error))
        s.commit()
        return {"series_scanned": len(scanned_series),
                "events": events_seen, "markets": markets_seen,
                "findings_new": created, "findings_expired": expired,
                "alerted": alerted, "error": error}
    except Exception as exc:
        s.rollback()
        print(f"[hunter] persist failed: {exc}")
        # the heartbeat must record the failure — a dead scanner must be
        # visible as dead, not as a quiet market
        try:
            s.add(HunterCycle(started_at=started, completed_at=_now(),
                              status="failed",
                              error=f"persist: {exc}"[:2000]))
            s.commit()
        except Exception:
            s.rollback()
        return {"error": str(exc)[:200]}
    finally:
        s.close()


# --- the public read surface ----------------------------------------------
def findings_report(competition: str | None = None,
                    finding_type: str | None = None,
                    status: str | None = None,
                    limit: int = 100) -> dict:
    """Findings WITH their denominators. A count without cycles-run /
    markets-scanned is a defect in this repo; the heartbeat age makes a
    dead scanner distinguishable from a quiet market."""
    if not plane_ready():
        return {"ready": False, "reason": "live plane dormant"}
    s = get_session()
    try:
        q = s.query(HunterFinding)
        if competition:
            comp = competition.strip()
            from sqlalchemy import or_
            q = q.filter(or_(HunterFinding.competition_slug == comp,
                             HunterFinding.series == comp.upper()))
        if finding_type:
            q = q.filter_by(finding_type=finding_type.strip().upper())
        if status:
            q = q.filter_by(status=status.strip().lower())
        rows = (q.order_by(HunterFinding.id.desc()).limit(limit).all())

        from sqlalchemy import func
        per_type = {ft: {"open": 0, "expired": 0} for ft in
                    ALERTABLE + CONTEXT_TYPES + ("MODEL_EDGE",)}
        for ft, st, n in (s.query(HunterFinding.finding_type,
                                  HunterFinding.status,
                                  func.count(HunterFinding.id))
                          .group_by(HunterFinding.finding_type,
                                    HunterFinding.status).all()):
            per_type.setdefault(ft, {"open": 0, "expired": 0})
            per_type[ft][st] = n
        cycles = s.query(func.count(HunterCycle.id)).scalar() or 0
        markets_total = s.query(
            func.coalesce(func.sum(HunterCycle.markets_scanned), 0)).scalar()
        last = (s.query(HunterCycle)
                .order_by(HunterCycle.id.desc()).first())
        last_age = None
        if last and last.completed_at:
            done = last.completed_at
            done = done if done.tzinfo else done.replace(tzinfo=timezone.utc)
            last_age = int((_now() - done).total_seconds())

        qual = _model_qualifier()
        return {
            "ready": True,
            "framing": SHADOW_FRAMING,
            "capture_clock": CAPTURE_CLOCK_NOTE,
            "fee_policy": FEE_POLICY["version"],
            "denominators": {
                "cycles_run": cycles,
                "markets_scanned_total": int(markets_total or 0),
                "last_cycle": None if last is None else {
                    "status": last.status,
                    "completed_at": (last.completed_at.isoformat()
                                     if last.completed_at else None),
                    "age_seconds": last_age,
                    "series_scanned": last.series_scanned,
                    "markets_scanned": last.markets_scanned,
                    "roster_size": last.roster_size,
                    "active_series": last.active_series,
                    "error": last.error,
                },
                "heartbeat_note": ("age_seconds far above the poll cadence "
                                   "means the scanner is DEAD, not that "
                                   "the market is quiet"),
            },
            "findings_per_type": per_type,
            "model_status": {
                "mls-2026": (qual if qual is not None else "no model"),
                "all_other_competitions": "no model",
            },
            "findings": [{
                "id": r.id,
                "competition_slug": r.competition_slug,
                "series": r.series,
                "event_ticker": r.event_ticker,
                "market_ticker": r.market_ticker,
                "finding_type": r.finding_type,
                "is_context": r.is_context,
                "context_note": ("liquidity context, not a win"
                                 if r.is_context else None),
                "net_margin_dollars": r.net_margin_dollars,
                "fee_policy_version": r.fee_policy_version,
                "legs": json.loads(r.legs_json) if r.legs_json else None,
                "model_qualifier": (json.loads(r.model_qualifier_json)
                                    if r.model_qualifier_json else None),
                "first_captured_at": (r.first_captured_at.isoformat()
                                      if r.first_captured_at else None),
                "last_seen_at": (r.last_seen_at.isoformat()
                                 if r.last_seen_at else None),
                "observed_cycles": r.observed_cycles,
                "espn_captured_at": (r.espn_captured_at.isoformat()
                                     if r.espn_captured_at else None),
                "status": r.status,
                "expired_at": (r.expired_at.isoformat()
                               if r.expired_at else None),
                "alerted_at": (r.alerted_at.isoformat()
                               if r.alerted_at else None),
            } for r in rows],
        }
    finally:
        s.close()
