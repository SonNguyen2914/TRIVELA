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
                  cached from a previous cycle. ORDER MATTERS: finality
                  is established FIRST and the priced quote is a FRESH
                  post-finality re-read, so kalshi_captured_at >=
                  espn_captured_at on every emitted row (a pre-final
                  price paired with a settled outcome manufactures a
                  margin that never existed). MLS only today — the only
                  competition with an approved fixture mapping.
  WIDE_SPREAD /   liquidity CONTEXT flags (config thresholds). Labelled
  THIN_BOOK       is_context=True; they are never wins and never alert.
                  A two-sided book with MISSING top-of-book size on
                  either side is liquidity_unknown — flagged, never
                  silently treated as adequately deep.
  MODEL_EDGE      readout ONLY where an ACTIVE approved model exists
                  (today: MLS). "Active" is the RUNTIME approval state:
                  the persisted approved decision AND
                  ModelVersion.approved_for_shadow, which boot
                  deliberately sets false after every deploy until an
                  operator activates. Each run must itself be bound to
                  that decision (model_approved_at_run=True and the
                  run's decision id equal to the published qualifier's)
                  — an unbound run yields no edge. Competitions without
                  an approval get "no model", never a number.
  IN_PLAY_        market-anchored repricing: the same market's mid moved
  OVERREACTION    more than a config threshold between two consecutive
                  hunter captures (capture-PAIRED: both of OUR clocks
                  stored) on a match dated today, AND the move exceeds
                  the wider of the two captures' spreads (spread-aware —
                  a "move" inside quote noise is not a repricing). The
                  PRIMARY signal is the market compared with ITSELF and
                  involves no model of ours at all — that is its whole
                  virtue and it is not replaced by anything below.
                  CONDITIONALITY DISCIPLINE: a violent in-play move is
                  usually conditioned on a real match event (goal, red
                  card), so the row reports which of THREE states it is
                  in, from API-Football's live feed where coverage allows
                  (src/live/apifootball_live.py):
                    conditioning_observed — a goal or red card was seen
                      at or before the move, so the market's reaction is
                      at least partly EXPLAINED, which makes it LESS
                      likely to be an overreaction and the row says so;
                    conditioning_unobserved_stats_available — the feed
                      was readable and shows no explaining event, the
                      strongest version of this finding;
                    conditioning_unobserved_no_stats — no readable feed,
                      exactly the behaviour that predates this and still
                      the common case across the taxonomy.
                  FINDING A REASON WEAKENS THE FINDING; it never
                  strengthens it, and CONDITIONING_STRENGTH pins that
                  ordering for a test rather than for prose. Live xG
                  rides ALONGSIDE the market anchor as a labelled
                  characterisation, never as a replacement or an
                  overrule. Still context-class; it alerts only if
                  HUNTER_IN_PLAY_ALERTS_ENABLED is turned on (default
                  OFF — that promotion is Son's call), and then only
                  through the same AMBIENT_DETAIL / detail-channel path.
                  The pair store is process-local: the first cycle after
                  a restart has no pair and detects nothing, recorded
                  honestly. It is bounded in AGE and SIZE
                  (_prune_pair_store) so it can never grow without limit.

Fee discipline (provider-aware, fail-closed): Kalshi declares a fee
schedule per SERIES (fee_type/fee_multiplier on the series object; the
market payload carries no fee field at all). The taxonomy is NOT
uniform — 90 of 97 soccer GAME series declare 'quadratic', the exact
formula this repo models, and 7 declare 'quadratic_with_maker_fees'
whose rate the API never publishes. Each finding is priced with its own
series' declared schedule; an unmodelled one is recorded as
`fee_unknown` with the GROSS structure only, carries no net margin and
can never alert. Discovery reads the schedules from the request it
already makes, so this costs no extra provider traffic.

Identity discipline (fail-closed): a market missing its event ticker or
its own ticker has no provider-stable identity, cannot be keyed, and
must never be swept into a synthetic event. One such row makes the
whole series' scan an IDENTITY_FAILED outcome for that cycle: zero
findings, zero alerts, expiry blocked, cycle degraded.

Completeness discipline (fail-closed): every scanned series gets a
recorded outcome — SUCCESS / REQUEST_FAILED / PAGINATION_CAP /
DETECTION_FAILED / IDENTITY_FAILED — persisted on the cycle row and
served by the API. "Didn't look" is never recordable as "no anomaly":
an open finding may only EXPIRE after a COMPLETE, SUCCESSFUL detection
pass over its series in the same cycle, a failed/truncated series stays
scheduled for the next cycle, and any non-SUCCESS outcome marks the
whole cycle degraded.

Chronological discipline (cross-process): Railway overlaps containers
deliberately, so an OLDER scan can commit AFTER a newer one. Every
write — insert, last-seen bump, expiry — first advances a per-key
observation watermark with an atomic
`UPDATE ... WHERE observed_through < :clock`. The DATABASE rejects the
stale writer, so a straggler can neither re-open an expired finding nor
alert on it; the rejection count rides on the cycle heartbeat rather
than disappearing.

Capture-clock discipline: Kalshi publishes no quote timestamp, so the
per-series fetch-COMPLETION clock is the ONLY timing evidence a quote
gets. That clock is threaded through every detector and persisted
verbatim (legs_json + first_captured_at); the detection clock, the ESPN
read clock and the persistence clock are stored separately and never
substituted for it.

Roster lag (documented trade): between discoveries (every
HUNTER_DISCOVERY_MINUTES, default 6h) only recently-active series are
scanned, so a series going active is picked up with at most that lag.
Failed series never leave the schedule through failure.

Provider facts this module is built on (verified, archived in
research_archive/kalshi_soccer_taxonomy_2026-07-28.json):
  - market payloads carry *_dollars price strings (exact Decimal) and
    *_fp size strings; integer-cent fields may be absent entirely;
  - the NO book is the mirrored YES book (no_bid == 1 - yes_ask); the
    payload has no no-side size fields, so no-side executable size is
    read from the mirrored YES side ONLY when the mirror identity holds;
  - Kalshi publishes NO quote timestamp (updated_time is the market
    DEFINITION clock, ~30h stale on active books);
  - Kalshi rate-limits hard: one throttled request stream, cursor
    pagination with a page cap (a surviving cursor at the cap is
    recorded as PAGINATION_CAP — scope incomplete), 429 backoff
    honouring Retry-After.

Alert discipline: only SUM_BELOW_ONE / CROSSED_BOOK / POST_CERTAINTY
above a config margin may alert, through the rule-based src.alerts path
(never the journal broadcast), and ONLY with positive executable size
proven on every required leg (P1-6). Delivery is durable and ordered:
the finding row is COMMITTED first, a leased per-row claim
(alert_claimed_at) is committed BEFORE any bytes leave the process, and
alerted_at is set — and the hourly budget consumed — only on a
CONFIRMED transport acceptance. All transports failing (or none
configured) releases the claim so the still-open finding retries next
cycle, with the failure detail retained on the row. Copy is
observational arithmetic — no imperatives, no "BUY"/"TAKE" — always
with the shadow-mode framing, rate-limited to HUNTER_ALERT_MAX_PER_HOUR
(the budget is per-process; the DB claim is what makes cross-process
duplicates impossible).
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
from src.live import apifootball_live
from src.live.db import get_session, plane_ready
from src.live.models import (HunterCycle, HunterFinding,
                             HunterObservationWatermark)
from src.live.paper import FEE_POLICY, order_fee_dollars

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAME_SERIES_SUFFIX = "GAME"
SOCCER_TAG = "Soccer"
MLS_SERIES = "KXMLSGAME"

ALERTABLE = ("SUM_BELOW_ONE", "CROSSED_BOOK", "POST_CERTAINTY")
CONTEXT_TYPES = ("WIDE_SPREAD", "THIN_BOOK", "IN_PLAY_OVERREACTION")

# per-series scan outcomes (fail-closed completeness accounting)
SERIES_SUCCESS = "SUCCESS"
SERIES_REQUEST_FAILED = "REQUEST_FAILED"
SERIES_PAGINATION_CAP = "PAGINATION_CAP"
SERIES_DETECTION_FAILED = "DETECTION_FAILED"
SERIES_IDENTITY_FAILED = "IDENTITY_FAILED"

# Findings never claim more than this framing allows. Attached to every
# alert and to the API report.
SHADOW_FRAMING = ("shadow mode — observational record only; no order was "
                  "or will be placed, and nothing here is advice")

CAPTURE_CLOCK_NOTE = ("all capture timestamps are OUR clock; Kalshi "
                      "publishes no quote timestamp (updated_time is the "
                      "definition clock)")

FEE_RESOLUTION_NOTE = (
    "each finding is priced with ITS OWN series' provider-declared fee "
    "schedule (kalshi series fee_type/fee_multiplier), never one policy "
    "assumed across the taxonomy; a schedule this repo does not model "
    "(e.g. quadratic_with_maker_fees, which 7 soccer series declare) is "
    "recorded as fee_unknown — gross structure only, no net margin, "
    "never alertable")

# Hard bounds so the scan can never fan out without limit.
MAX_SERIES_PER_CYCLE = 120
MAX_PAGES_PER_SERIES = 3
PAGE_LIMIT = 200

# How long a committed alert claim (alert_claimed_at) stays exclusive
# before another process may reclaim it. A process that dies between
# claim and delivery leaves a stale claim that EXPIRES rather than
# muting the finding forever; a live process either confirms delivery
# (alerted_at) or releases the claim itself.
ALERT_CLAIM_LEASE_MINUTES = 30

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


def _paged_markets(series: str, counter: dict) -> tuple[list[dict], bool]:
    """Open markets for one series, cursor-paged with a hard cap.

    Returns (markets, truncated). truncated=True means the cursor was
    still live when the page cap was reached — the scope is INCOMPLETE
    and the caller must record it (PAGINATION_CAP), never treat the
    partial list as the whole book (P0-1)."""
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
    return out, bool(cursor)


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


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


# --- provider-declared fee schedules (P0-1) --------------------------------
# Kalshi publishes a market's fee schedule on its SERIES object
# (fee_type + fee_multiplier) and NOWHERE on the market payload —
# verified 2026-07-29, archived in
# research_archive/kalshi_fee_schedules_2026-07-29.json. The soccer
# taxonomy is NOT uniform: of 97 soccer GAME series, 90 declare
# 'quadratic' — the exact general-taker formula this repo models — and
# 7 declare 'quadratic_with_maker_fees': KXWCGAME, KXUCLGAME,
# KXEPLGAME, KXLALIGAGAME, KXBUNDESLIGAGAME, KXLIGUE1GAME,
# KXSERIEAGAME. Assuming ONE schedule across the taxonomy is how a
# "true structural arbitrage" gets computed with the wrong fee.
#
# The provider publishes the NAME of a schedule, never its rate.
# 'quadratic' at multiplier 1 is the one we can price exactly
# (FEE_POLICY); anything else is UNKNOWN and stays unpriced —
# FEE_POLICY itself declares maker fees, exit fees and series overrides
# NOT modelled, and inventing a rate for a name is precisely the error
# this guard exists to prevent.
FEE_TYPE_MODELED = "quadratic"
FEE_MULTIPLIER_MODELED = 1
FEE_UNKNOWN_PREFIX = "kalshi-fee-unknown"
FEE_SOURCE_NOTE = ("kalshi SERIES object (fee_type/fee_multiplier); the "
                   "market payload carries no fee field at all")


class FeeSchedule:
    """One series' fee schedule, as the PROVIDER declares it.

    `modeled` is True ONLY for fee_type 'quadratic' at multiplier 1.
    Every other declared type, an unexpected multiplier, and a schedule
    that could not be read at all are fee_unknown: `fee()` returns None,
    the finding records the gross structure with `fee_status`
    'fee_unknown' and NO net margin, and `_alert_eligible` refuses it.
    A structural finding priced with the wrong fee schedule is a false
    finding, so an unpriceable book is recorded and never alerted."""

    def __init__(self, series: str, fee_type=None, multiplier=None,
                 reason: str | None = None):
        self.series = series
        self.fee_type = fee_type
        self.multiplier = multiplier
        if reason is not None:
            self.reason = reason
        elif fee_type is None:
            self.reason = ("no fee schedule was read from the provider for "
                           "this series")
        elif fee_type != FEE_TYPE_MODELED:
            self.reason = (
                f"provider fee_type '{fee_type}' is not modelled; Kalshi "
                "publishes the schedule NAME but not its rate, and "
                f"{FEE_POLICY['not_modeled']}")
        elif multiplier != FEE_MULTIPLIER_MODELED:
            self.reason = (f"provider fee_multiplier {multiplier!r} is not "
                           "modelled (only 1 is)")
        else:
            self.reason = None

    @property
    def modeled(self) -> bool:
        return self.reason is None

    def fee(self, price: Decimal) -> Decimal | None:
        """Exact fee for ONE contract at `price` under THIS series'
        declared schedule — the repo's Decimal fee module, never a float
        approximation — or None when the schedule is unknown."""
        if not self.modeled:
            return None
        return order_fee_dollars(price, 1)

    @property
    def version(self) -> str:
        if self.modeled:
            return FEE_POLICY["version"]
        return f"{FEE_UNKNOWN_PREFIX}:{self.fee_type or 'unread'}"[:64]

    @property
    def status(self) -> str:
        return "modeled" if self.modeled else "fee_unknown"

    def block(self) -> dict:
        """What lands verbatim in legs_json, so a stored finding always
        says which schedule its arithmetic used and where it came from."""
        out = {
            "fee_status": self.status,
            "fee_policy": self.version,
            "provider_fee_type": self.fee_type,
            "provider_fee_multiplier": self.multiplier,
            "source": FEE_SOURCE_NOTE,
        }
        if not self.modeled:
            out["reason"] = self.reason
            out["note"] = ("fee_unknown: no net margin is asserted and this "
                           "finding can NEVER alert — the gross structure "
                           "is recorded as an observation only")
        return out


def _fee_schedule(series: str) -> FeeSchedule:
    """The resolved schedule for one series. Absent from the roster map
    (discovery failed, series pinned without a readable series object,
    provider omitted the field) means UNKNOWN, never 'assume general'."""
    sched = (_roster.get("fee") or {}).get(series)
    if not sched:
        return FeeSchedule(series)
    return FeeSchedule(series, fee_type=sched.get("fee_type"),
                       multiplier=sched.get("fee_multiplier"))


def _leg_liquidity(sizes: dict[str, Decimal | None]
                   ) -> tuple[bool, str | None, dict]:
    """Executable-size audit for a structural finding's required legs.

    Returns (alertable, limiting_quantity, liquidity_block). Alertable
    ONLY when every required leg has a KNOWN, POSITIVE top-of-book size;
    a missing size is liquidity_unknown and a zero size is zero_size —
    neither may ever pass as adequately deep (P1-6). The limiting
    quantity (min across legs) rides with the finding so the margin is
    never read as executable beyond it."""
    block = {k: _s(v) for k, v in sizes.items()}
    missing = sorted(k for k, v in sizes.items() if v is None)
    zero = sorted(k for k, v in sizes.items() if v is not None and v <= 0)
    if missing or zero:
        return False, None, {
            "status": "liquidity_unknown" if missing else "zero_size",
            "reasons": ([f"{k}_missing" for k in missing]
                        + [f"{k}_not_positive" for k in zero]),
            "sizes": block,
            "note": ("NOT alertable: a structural finding needs a "
                     "positive executable size proven on every required "
                     "leg; missing size is unknown liquidity, never "
                     "assumed depth"),
        }
    limiting = min(sizes.values())
    return True, str(limiting), {
        "status": "sized", "sizes": block,
        "limiting_quantity": str(limiting),
        "note": "executable at top-of-book up to limiting_quantity "
                "contracts",
    }


# --- roster discovery ------------------------------------------------------
# Module-level cache: refreshed every HUNTER_DISCOVERY_MINUTES. Between
# discoveries only series that recently had open markets are scanned, so
# the steady-state request budget stays proportional to live soccer, not
# to the whole taxonomy. Documented consequence: a series going active
# between discoveries is seen with at most that lag (≤6h default). A
# series NEVER leaves the schedule through failure — only a complete
# successful scan that saw no open markets drops it (P0-1).
_roster: dict = {"at": None, "series": [], "active": set(), "fee": {}}


def _fetch_series_fee_schedule(ticker: str, counter: dict) -> dict | None:
    """One series' declared fee schedule, for a roster PINNED via
    config.HUNTER_SERIES (which bypasses discovery entirely and so
    never sees the bulk listing). A failure returns None — the series
    then has no readable schedule and its findings are fee_unknown,
    never silently priced as general-taker."""
    try:
        d = _get(f"{KALSHI}/series/{ticker}")
        counter["requests"] = counter.get("requests", 0) + 1
    except requests.RequestException as exc:
        print(f"[hunter] fee schedule read failed for {ticker}: {exc}")
        return None
    s = d.get("series") or {}
    if not s.get("fee_type"):
        return None
    return {"fee_type": s.get("fee_type"),
            "fee_multiplier": s.get("fee_multiplier")}


def discover_roster(counter: dict) -> list[str] | None:
    """The soccer per-match series roster, discovered empirically from
    the provider's series listing (tag Soccer, ticker ends GAME) minus
    the config skip-list. Config HUNTER_SERIES overrides discovery
    entirely. Returns None on failure so a stale roster is kept rather
    than silently scanning nothing (the failure is recorded on the
    cycle row as discovery_error).

    Each series' FEE SCHEDULE is read here too (P0-1). The bulk listing
    already carries fee_type/fee_multiplier per series, so the
    discovered path costs ZERO extra provider requests; a pinned roster
    pays one cheap series read each, once per discovery window."""
    if config.HUNTER_SERIES:
        out = list(config.HUNTER_SERIES)
        fees = {}
        for t in out:
            sched = _fetch_series_fee_schedule(t, counter)
            if sched:
                fees[t] = sched
        _roster["fee"] = fees
        return out
    try:
        d = _get(f"{KALSHI}/series", {"category": "Sports"})
        counter["requests"] = counter.get("requests", 0) + 1
    except requests.RequestException as exc:
        print(f"[hunter] roster discovery failed: {exc}")
        return None
    skip = set(config.HUNTER_SERIES_SKIP)
    listing = [s for s in (d.get("series") or [])
               if s.get("ticker")
               and s["ticker"].endswith(GAME_SERIES_SUFFIX)
               and SOCCER_TAG in (s.get("tags") or [])
               and s["ticker"] not in skip]
    out = sorted({s["ticker"] for s in listing})
    # the same payload the roster came from declares each series' fee
    # schedule — read it, never assume one schedule for the taxonomy
    _roster["fee"] = {s["ticker"]: {"fee_type": s.get("fee_type"),
                                    "fee_multiplier": s.get("fee_multiplier")}
                      for s in listing if s.get("fee_type")}
    return out or None


# --- detectors -------------------------------------------------------------
def _tie_leg(ticker: str) -> bool:
    return (ticker or "").rsplit("-", 1)[-1].upper() in ("TIE", "DRAW")


def detect_event_findings(series: str, event_ticker: str,
                          markets: list[dict], captured_at: datetime,
                          fees: FeeSchedule) -> list[dict]:
    """SUM_BELOW_ONE for one event plus the per-market detectors
    (CROSSED_BOOK and the liquidity context flags). Pure: in-memory
    market dicts in, finding dicts out. `captured_at` is the per-series
    fetch-completion clock and is preserved verbatim on every finding
    (top-level datetime + legs isoformat) — P0-4.

    `fees` is THIS series' provider-declared schedule (P0-1). When it is
    unknown the structural detectors still record the GROSS structure —
    "didn't look" is never "no anomaly" — but assert no net margin and
    can never alert."""
    found: list[dict] = []
    cap = captured_at.isoformat()
    fee_block = fees.block()

    # SUM_BELOW_ONE — only on a provable full partition: exactly three
    # legs, exactly one TIE/DRAW leg, every leg with a real ask.
    ties = [m for m in markets if _tie_leg(m.get("ticker", ""))]
    if len(markets) == 3 and len(ties) == 1:
        asks = [(m, _price(m, "yes_ask")) for m in markets]
        if all(a is not None for _, a in asks):
            total_ask = sum(a for _, a in asks)
            gross = ONE - total_ask
            legs = [(m, a, fees.fee(a)) for m, a in asks]
            total_fee = (sum(f for _, _, f in legs) if fees.modeled
                         else None)
            margin = None if total_fee is None else gross - total_fee
            # modelled: the NET margin must clear zero. unknown: the
            # gross structure alone is the observation, never a claim
            if gross > 0 and (margin is None or margin > 0):
                sizes = {
                    f"yes_ask_size:{m.get('ticker')}":
                        _size(m, "yes_ask_size") for m, _, _ in legs}
                alertable, limiting, liq = _leg_liquidity(sizes)
                found.append({
                    "finding_type": "SUM_BELOW_ONE",
                    "series": series, "event_ticker": event_ticker,
                    "market_ticker": None, "is_context": False,
                    "net_margin_dollars": _s(margin),
                    "gross_margin_dollars": str(gross),
                    "fee_status": fees.status,
                    "fee_policy_version": fees.version,
                    "captured_at": captured_at, "detected_at": _now(),
                    "alertable_liquidity": alertable,
                    "limiting_quantity": limiting,
                    "legs": {
                        "rule": ("1 - sum(yes_ask) - sum(fee_per_leg) > 0 "
                                 "over a full 3-way partition"
                                 if fees.modeled else
                                 "1 - sum(yes_ask) > 0 over a full 3-way "
                                 "partition; fees UNPRICEABLE for this "
                                 "series, so no net claim is made"),
                        "captured_at": cap,
                        "legs": [{"ticker": m.get("ticker"),
                                  "yes_ask_dollars": _s(a),
                                  "yes_ask_size": _s(
                                      _size(m, "yes_ask_size")),
                                  "fee_dollars": _s(f)}
                                 for m, a, f in legs],
                        "sum_asks_dollars": str(total_ask),
                        "sum_fees_dollars": _s(total_fee),
                        "payout_dollars": "1",
                        "gross_margin_dollars": str(gross),
                        "net_margin_dollars": _s(margin),
                        "liquidity": liq,
                        "fee_schedule": fee_block,
                        "fee_policy": fees.version,
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
                fee_a = fees.fee(ONE - yes_bid)
                fee_b = fees.fee(ONE - no_bid)
                margin = (None if not fees.modeled
                          else gross - fee_a - fee_b)
                if margin is None or margin > 0:
                    # executable sizes: the yes_bid leg consumes the
                    # yes_bid side; the no_bid leg is the MIRRORED
                    # yes_ask side (no_bid == 1 - yes_ask; Kalshi
                    # publishes no no-side sizes), readable only when
                    # the mirror identity actually holds.
                    bid_size = _size(m, "yes_bid_size")
                    mirror_ok = (yes_ask is not None
                                 and no_bid + yes_ask == ONE)
                    no_side = (_size(m, "yes_ask_size")
                               if mirror_ok else None)
                    alertable, limiting, liq = _leg_liquidity({
                        "yes_bid_size": bid_size,
                        "no_bid_side_size": no_side})
                    liq["no_bid_size_basis"] = (
                        "mirrored yes_ask side (no_bid == 1 - yes_ask "
                        "verified)" if mirror_ok else
                        "UNKNOWN — mirror identity no_bid == 1 - "
                        "yes_ask did not hold, no no-side size exists")
                    found.append({
                        "finding_type": "CROSSED_BOOK",
                        "series": series, "event_ticker": event_ticker,
                        "market_ticker": tick, "is_context": False,
                        "net_margin_dollars": _s(margin),
                        "gross_margin_dollars": str(gross),
                        "fee_status": fees.status,
                        "fee_policy_version": fees.version,
                        "captured_at": captured_at,
                        "detected_at": _now(),
                        "alertable_liquidity": alertable,
                        "limiting_quantity": limiting,
                        "legs": {
                            "rule": ("yes_bid + no_bid - 1 - fee(1-yes_bid)"
                                     " - fee(1-no_bid) > 0" if fees.modeled
                                     else "yes_bid + no_bid - 1 > 0; fees "
                                     "UNPRICEABLE for this series, so no "
                                     "net claim is made"),
                            "captured_at": cap,
                            "yes_bid_dollars": _s(yes_bid),
                            "no_bid_dollars": _s(no_bid),
                            "fee_leg_yes_dollars": _s(fee_a),
                            "fee_leg_no_dollars": _s(fee_b),
                            "gross_margin_dollars": str(gross),
                            "net_margin_dollars": _s(margin),
                            "liquidity": liq,
                            "fee_schedule": fee_block,
                            "fee_policy": fees.version,
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
                "captured_at": captured_at, "detected_at": _now(),
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
            # a MISSING top-of-book size on a two-sided book is
            # liquidity_unknown — flagged explicitly, never silently
            # passed as adequately deep (P1-6)
            for side, sz in (("yes_ask_size", ask_size),
                             ("yes_bid_size", bid_size)):
                if sz is None:
                    thin_reasons.append(
                        f"{side}_missing:liquidity_unknown")
                elif sz < min_size:
                    thin_reasons.append(f"{side}_below_{min_size}")
        if thin_reasons:
            found.append({
                "finding_type": "THIN_BOOK",
                "series": series, "event_ticker": event_ticker,
                "market_ticker": tick, "is_context": True,
                "net_margin_dollars": None,
                "captured_at": captured_at, "detected_at": _now(),
                "legs": {"rule": "top-of-book size below threshold, "
                                 "missing (liquidity_unknown) or "
                                 "one-sided book (context flag, not a "
                                 "win)",
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
# finding's arithmetic is persisted in full when one fires. Bounded in
# age and size by _prune_pair_store — see below.
_pair_store: dict[str, dict] = {}

# a pair older than this cannot honestly be called "consecutive
# captures" (and a ticker unseen this long is usually settled/closed);
# the size cap is a hard memory bound, oldest evicted first
_PAIR_STORE_MAX_AGE_S = 6 * 3600
_PAIR_STORE_MAX_ENTRIES = 20_000

_TICKER_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})")


def _prune_pair_store(now: datetime) -> None:
    """Bound the process-local pair store in AGE and SIZE. Aged entries
    are dropped (the ticker's next appearance re-seeds its pair — one
    blind cycle, same as after a restart, recorded honestly by the
    detector simply not firing); above the entry cap the oldest
    captures are evicted first."""
    cutoff = (now - timedelta(seconds=_PAIR_STORE_MAX_AGE_S)).isoformat()
    for tick in [t for t, v in _pair_store.items()
                 if not v.get("captured_at") or v["captured_at"] < cutoff]:
        _pair_store.pop(tick, None)
    over = len(_pair_store) - _PAIR_STORE_MAX_ENTRIES
    if over > 0:
        oldest = sorted(_pair_store,
                        key=lambda t: _pair_store[t].get("captured_at")
                        or "")[:over]
        for tick in oldest:
            _pair_store.pop(tick, None)


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
    a correct instant repricing of a match event, so the finding is
    context and never claims mispricing.

    PURE by design — no provider I/O here. The row leaves this function
    in the `conditioning_unobserved_no_stats` state, which is the honest
    default and is what it stays in for every competition without live
    coverage. `enrich_overreactions()` is the only thing that upgrades
    it, and only from a reading it actually took."""
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
    prev_at = prev.get("captured_at")
    window_s = None
    if prev_at:
        try:
            window_s = (captured_at - datetime.fromisoformat(prev_at)
                        ).total_seconds()
        except (ValueError, TypeError):
            window_s = None
    return {
        "finding_type": "IN_PLAY_OVERREACTION",
        "series": series, "event_ticker": event_ticker,
        "market_ticker": m.get("ticker"), "is_context": True,
        "net_margin_dollars": None,
        "captured_at": captured_at, "detected_at": _now(),
        # the capture window in minutes, so the live-evidence layer can
        # relate MATCH minutes to the wall-clock interval the move
        # happened in. An estimate, and labelled one where it is used.
        "capture_window_minutes": (None if window_s is None
                                   else round(window_s / 60.0, 2)),
        "legs": {
            "rule": (f"|mid_now - mid_prev| >= {min_move} AND > "
                     "max(spread_prev, spread_now), event dated today "
                     "(context flag, not a win)"),
            "primary_signal": (
                "MARKET-ANCHORED and model-free: this market's mid "
                "compared with its OWN previous mid. No model of ours "
                "appears in it, which is exactly why it is the primary — "
                "it cannot be wrong about a model because it contains "
                "none. Any live-evidence block below sits BESIDE this "
                "arithmetic and never replaces or overrules it"),
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
            # the honest DEFAULT state. Upgraded only by
            # enrich_overreactions(), and only from a reading actually
            # taken — never by assuming coverage exists.
            "conditioning": {
                "state": apifootball_live.CONDITIONING_UNOBSERVED_NO_STATS,
                "reason": ("no live-evidence pass has been applied to this "
                           "finding (live stats disabled, no key "
                           "configured, or no reading taken)"),
                "note": apifootball_live.CONDITIONING_NOTE[
                    apifootball_live.CONDITIONING_UNOBSERVED_NO_STATS],
                "strength_rank": apifootball_live.CONDITIONING_STRENGTH[
                    apifootball_live.CONDITIONING_UNOBSERVED_NO_STATS],
            },
            "conditionality": ("the conditioning match event (goal, red "
                               "card, abandonment...) may be OBSERVED or "
                               "UNOBSERVED — read the `conditioning` "
                               "block, which states which. A large move "
                               "may be a CORRECT instant repricing; this "
                               "row claims a move happened, never that "
                               "the market is wrong"),
        }}


def enrich_overreactions(s, findings: list[dict],
                         event_market_tickers: dict[str, list[str]],
                         now: datetime) -> dict:
    """Attach OBSERVED conditioning evidence to IN_PLAY_OVERREACTION rows.

    This is the only place the API-Football live endpoints are touched,
    and it is deliberately the narrowest possible surface:

      - it runs ONLY when at least one overreaction candidate exists, so
        a quiet cycle spends ZERO provider requests. The scanner does not
        sweep the world;
      - it pays 1 request for every in-play fixture on earth, then 2 per
        DISTINCT fixture a Kalshi market actually resolved to;
      - a competition measured ABSENT inside its TTL is skipped without
        spending anything, and the skip is recorded on the finding as a
        cached verdict WITH its measurement date — never as a fresh
        reading;
      - every failure downgrades the finding to the honest no-feed state.
        Nothing here may turn a failed read into a conclusion.

    Returns a report block for the cycle row: what was spent, what
    resolved, and what did not.

    LEAKAGE: these readings reach `HunterFinding.legs_json` and the
    coverage table only. Live xG carries post-lock information and must
    never enter a lock, a PredictionRun, a paper signal, or a fitted or
    scored feature.
    """
    report: dict = {"ran": False, "candidates": 0, "requests": 0}
    candidates = [f for f in findings
                  if f.get("finding_type") == "IN_PLAY_OVERREACTION"]
    report["candidates"] = len(candidates)
    if not candidates:
        report["skipped"] = "no overreaction candidate this cycle"
        return report
    if not config.HUNTER_LIVE_STATS_ENABLED:
        report["skipped"] = "HUNTER_LIVE_STATS_ENABLED is false"
        return report
    key, key_source = apifootball_live.load_key()
    if not key:
        # the deliberate inert state: deploying this changes nothing until
        # an operator configures a key
        report["skipped"] = (
            f"no API-Football key configured (source: {key_source}) — the "
            "live-evidence pass is INERT and every finding keeps the "
            "honest conditioning_unobserved_no_stats state")
        return report

    client = apifootball_live.LiveClient(key)
    report["ran"] = True
    try:
        index = apifootball_live.fetch_live_index(client)
    except Exception as exc:
        report["error"] = apifootball_live.redact(
            f"live index read failed: {type(exc).__name__}: {exc}")
        report["requests"] = client.used
        report["budget"] = client.budget_block()
        print(f"[hunter] live index read failed: {type(exc).__name__}")
        return report
    report["fixtures_live_total"] = index.get("fixtures_live_total")
    if index.get("count_disagreement"):
        report["count_disagreement"] = index["count_disagreement"]

    apifootball_live.prune_reading_store(now)
    joins: dict[str, dict] = {}
    readings: dict[str, dict] = {}
    states: dict[str, int] = {}
    # This cycle's readings are stored only AFTER the loop, and the
    # PREVIOUS one is snapshotted per fixture on first sight. Storing
    # inside the loop was wrong: two legs of one event are two candidates
    # on ONE fixture, so the second would read the first's just-stored
    # reading as its "previous" and compute an xG window delta of zero —
    # a fabricated "nothing happened in this window", from comparing a
    # reading with itself.
    prev_readings: dict[str, dict | None] = {}
    for f in candidates:
        ev = f.get("event_ticker") or ""
        if ev not in joins:
            joins[ev] = apifootball_live.resolve_fixture(
                f.get("series") or "", event_market_tickers.get(ev) or [],
                index)
        join = joins[ev]
        reading = None
        if join.get("status") == apifootball_live.JOIN_RESOLVED:
            fid = str(join.get("fixture_id"))
            if fid not in prev_readings:
                prev_readings[fid] = apifootball_live.previous_reading(fid)
            if fid in readings:
                reading = readings[fid]
            elif apifootball_live.coverage_is_fresh_absent(
                    s, join.get("league_id"), now):
                # measured absent recently: skip the spend, and say so on
                # the row as a CACHED verdict rather than a reading
                join = dict(join)
                join["coverage_shortcut"] = (
                    "this competition was MEASURED to serve no live "
                    "statistics within the coverage TTL, so no request was "
                    "spent. This is a CACHED verdict, not a reading of "
                    "this fixture — see /api/admin/hunter/live-coverage "
                    "for the date it was measured")
                joins[ev] = join
            else:
                try:
                    reading = apifootball_live.fetch_reading(
                        client, join.get("fixture_id"))
                    readings[fid] = reading
                    try:
                        apifootball_live.record_coverage(
                            s, join.get("league_id"),
                            join.get("league_name"),
                            join.get("league_country"),
                            reading, join.get("elapsed"), now)
                    except Exception as exc:
                        # coverage bookkeeping must never cost a finding
                        print("[hunter] coverage upsert failed: "
                              f"{type(exc).__name__}")
                except apifootball_live.BudgetExceeded as exc:
                    report.setdefault("budget_aborts", []).append(str(exc))
                except Exception as exc:
                    report.setdefault("reading_errors", []).append(
                        apifootball_live.redact(
                            f"{join.get('fixture_id')}: "
                            f"{type(exc).__name__}"))
        cond = apifootball_live.classify_conditioning(
            reading, join, elapsed=join.get("elapsed"),
            window_minutes=f.get("capture_window_minutes"))
        f["legs"]["conditioning"] = cond
        f["legs"]["live_xg_variant"] = (
            apifootball_live.live_xg_characterisation(
                reading, join,
                prev_readings.get(str(join.get("fixture_id")))))
        f["legs"]["signal_precedence"] = (
            "PRIMARY = the market-anchored capture_pair arithmetic above, "
            "model-free. SECONDARY = live_xg_variant, a characterisation "
            "of live evidence. They are reported separately and are NOT "
            "reconciled into one verdict: if they disagree, the "
            "market-anchored primary is what this finding is made of, and "
            "the disagreement is itself the interesting part")
        f["legs"]["conditioning_states_available"] = (
            list(apifootball_live.CONDITIONING_STATES))
        states[cond["state"]] = states.get(cond["state"], 0) + 1

    # AFTER the loop, so no candidate in this cycle can see another
    # candidate's reading of the same fixture as its "previous"
    for fid, reading in readings.items():
        apifootball_live.remember_reading(fid, reading)

    report["conditioning_states"] = states
    report["joins"] = {ev: j.get("status") for ev, j in joins.items()}
    report["fixtures_read"] = len(readings)
    report["requests"] = client.used
    report["budget"] = client.budget_block()
    return report


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


def _refetch_market(ticker: str) -> tuple[dict | None, datetime]:
    """Re-read ONE market and stamp OUR fetch-completion clock (P0-2).

    POST_CERTAINTY pairs a Kalshi quote with an ESPN full-time read, so
    the quote must be captured AFTER finality was observed — a price
    from before the final whistle plus a settled outcome manufactures a
    margin that never existed. This is the fresh read; it happens only
    for a market whose match ESPN has just reported final, so the extra
    provider traffic is bounded by finished matches, not by the roster.
    Throttled through the one shared pacing clock like every other
    hunter request."""
    d = _get(f"{KALSHI}/markets/{ticker}")
    at = _now()
    m = d.get("market")
    return (m if isinstance(m, dict) else None), at


def _clocks_ordered(f: dict) -> bool:
    """The P0-2 invariant on an emitted POST_CERTAINTY row: the Kalshi
    quote clock must not precede the ESPN finality clock. Checked here,
    again at the composed cycle layer, and enforced a third time by a
    CHECK constraint on the table itself."""
    espn_at = f.get("espn_captured_at")
    cap = f.get("captured_at")
    if espn_at is None or cap is None:
        return True
    return cap >= espn_at


def _fixture_local_date(dt: datetime) -> str:
    from zoneinfo import ZoneInfo
    d = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return d.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def detect_post_certainty(s, markets_by_ticker: dict[str, dict],
                          captured_at: datetime, fees: FeeSchedule,
                          counter: dict | None = None
                          ) -> tuple[list[dict], bool]:
    """Matches ESPN says are FINISHED whose mapped Kalshi market still
    trades the certain outcome below $1 - fee.

    MLS only: it is the one competition with an approved fixture mapping.
    The ESPN state is fetched HERE, at detection time — never reused from
    an earlier cycle — and the outcome is derived from the per-side score
    NUMBERS (ingest._event_to_fields), never a composite string.

    ORDER OF OBSERVATION (P0-2): finality is established FIRST, and only
    then is the quote captured — a FRESH per-market re-read whose clock
    is necessarily later than the ESPN read. `captured_at` (the cycle's
    MLS fetch-completion clock) is what made the market a candidate and
    is retained as `pre_finality_kalshi_captured_at` evidence, but it is
    NEVER the quote the arithmetic uses: pairing a pre-final price with
    a settled outcome manufactures a margin that never existed. Every
    emitted row satisfies kalshi_captured_at >= espn_captured_at.

    Returns (findings, complete): complete=False when ANY ESPN date-page
    read failed, any post-finality re-quote failed, or any row failed
    the clock ordering — a partial pass must never be recorded as "no
    anomaly", and the caller marks the series DETECTION_FAILED so its
    open findings cannot expire this cycle (P0-1)."""
    from src.live.ingest import _event_to_fields
    from src.live.models import Fixture, MarketContract, MarketEvent

    found: list[dict] = []
    complete = True
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
        return found, complete

    by_date: dict[str, list] = {}
    for me, fx, ko in candidates:
        by_date.setdefault(_fixture_local_date(ko), []).append((me, fx))

    for date_str, items in by_date.items():
        try:
            payload = _fetch_espn_scoreboard(date_str)
        except requests.RequestException as exc:
            print(f"[hunter] espn re-read {date_str} failed: {exc}")
            complete = False             # "didn't look" ≠ "no anomaly"
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
            pre = markets_by_ticker[mc.ticker]
            # P0-2: finality is now established — capture the quote
            # AFTER it. The cycle's pre-final quote only made this
            # market a candidate; it may never price the finding.
            try:
                m, quote_at = _refetch_market(mc.ticker)
                if counter is not None:
                    counter["requests"] = counter.get("requests", 0) + 1
            except requests.RequestException as exc:
                print(f"[hunter] post-final re-quote {mc.ticker} "
                      f"failed: {exc}")
                complete = False      # "didn't look" ≠ "no anomaly"
                continue
            if m is None:
                complete = False
                continue
            if quote_at < espn_at:    # clock skew — refuse to emit
                print(f"[hunter] post-final re-quote {mc.ticker} clock "
                      "precedes the ESPN read; refusing the row")
                complete = False
                continue
            ask = _price(m, "yes_ask")
            if ask is None:
                continue
            fee = fees.fee(ask)
            gross = ONE - ask
            margin = None if fee is None else gross - fee
            if gross > 0 and (margin is None or margin > 0):
                alertable, limiting, liq = _leg_liquidity(
                    {"yes_ask_size": _size(m, "yes_ask_size")})
                found.append({
                    "finding_type": "POST_CERTAINTY",
                    "series": MLS_SERIES,
                    "competition_slug": "mls-2026",
                    "event_ticker": me.kalshi_event_ticker,
                    "market_ticker": mc.ticker,
                    "fixture_id": fx.id,
                    "is_context": False,
                    "net_margin_dollars": _s(margin),
                    "gross_margin_dollars": str(gross),
                    "fee_status": fees.status,
                    "fee_policy_version": fees.version,
                    "captured_at": quote_at,
                    "detected_at": _now(),
                    "espn_captured_at": espn_at,
                    "alertable_liquidity": alertable,
                    "limiting_quantity": limiting,
                    "legs": {
                        "rule": ("ESPN state 'post' at detection-time "
                                 "re-read, THEN a fresh post-finality "
                                 "quote; certain outcome still asked "
                                 "below 1 - fee"),
                        "kalshi_captured_at": quote_at.isoformat(),
                        "espn_captured_at": espn_at.isoformat(),
                        "clock_order": ("kalshi_captured_at >= "
                                        "espn_captured_at: the quote was "
                                        "captured AFTER finality was "
                                        "observed"),
                        "pre_finality_kalshi_captured_at":
                            captured_at.isoformat(),
                        "pre_finality_yes_ask_dollars":
                            _s(_price(pre, "yes_ask")),
                        "pre_finality_note": ("the cycle's earlier quote; "
                                              "it made this market a "
                                              "candidate and is NOT part "
                                              "of the arithmetic"),
                        "espn_event_id": fx.espn_event_id,
                        "final_score_home": hg, "final_score_away": ag,
                        "certain_outcome": outcome,
                        "yes_ask_dollars": _s(ask),
                        "fee_dollars": _s(fee),
                        "gross_margin_dollars": str(gross),
                        "net_margin_dollars": _s(margin),
                        "liquidity": liq,
                        "fee_schedule": fees.block(),
                        "fee_policy": fees.version,
                    }})
    return found, complete


def _model_state() -> tuple[dict | None, str]:
    """(qualifier, status) for the MLS model.

    The qualifier exists ONLY when BOTH hold (P0-3):
      (a) an immutable APPROVED decision is persisted
          (model_eval.current_approval_decision), AND
      (b) the RUNTIME approval state is on —
          ModelVersion.approved_for_shadow, which boot deliberately sets
          FALSE after every deploy until an operator activates.

    During the fail-closed window the newest persisted decision is NOT
    the operating state: shadow runs are refused, so MODEL_EDGE goes
    dark with them ("no active model") instead of quoting a decision the
    runtime itself refuses to run under. Never recomputed, never
    hardcoded."""
    try:
        from src.live import model_eval
        dec = model_eval.current_approval_decision()
    except Exception as exc:
        print(f"[hunter] approval read failed: {exc}")
        return None, "no model (approval state unreadable)"
    if not dec or dec.get("approval_decision_missing"):
        return None, "no model"
    try:
        from src.live.models import ModelVersion
        s = get_session()
        try:
            name = dec.get("model_version")
            mv = (s.query(ModelVersion).filter_by(name=name).first()
                  if name else None)
            active = bool(mv and mv.approved_for_shadow)
        finally:
            s.close()
    except Exception as exc:
        print(f"[hunter] runtime approval read failed: {exc}")
        return None, "no model (approval state unreadable)"
    if not active:
        return None, ("no active model — an approved decision exists but "
                      "the runtime approval (approved_for_shadow) is OFF; "
                      "boot fails closed after every deploy until operator "
                      "activation")
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
    }, "active"


def detect_model_edge(s, markets_by_ticker: dict[str, dict],
                      captured_at: datetime,
                      fees: FeeSchedule) -> list[dict]:
    """MODEL_EDGE readouts for mapped, upcoming MLS 3-way contracts.

    ONLY where an ACTIVE approved model exists (P0-3): the persisted
    approval decision AND the runtime approved_for_shadow flag, AND each
    run must itself be bound to that exact decision —
    model_approved_at_run=True and the run's model_approval_decision_id
    EQUAL to the published qualifier's decision_id. A run minted outside
    the approval (or under an older decision) yields no edge, fail
    closed. The probabilities are the stored output of the existing
    machinery (PredictionRun / PredictionContract) and the fee is the
    exact fee module — nothing is re-modelled here. Observational;
    never alerts."""
    from src.live.models import (Fixture, MarketContract, MarketEvent,
                                 PredictionContract, PredictionRun)
    qualifier, _status = _model_state()
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
        # P0-3 runtime binding: the run must carry its own
        # contemporaneous approval AND point at the SAME decision the
        # qualifier publishes — an edge under decision N must never be
        # emitted from a run authorized under decision M (or none).
        if not run.model_approved_at_run:
            continue
        if run.model_approval_decision_id != qualifier.get("decision_id"):
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
            fee = fees.fee(ask)
            if fee is None:
                # an unpriceable schedule cannot produce a net edge
                # readout either — fail closed rather than quote a
                # number computed with the wrong fee (P0-1)
                continue
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
                "fee_status": fees.status,
                "fee_policy_version": fees.version,
                "captured_at": captured_at, "detected_at": _now(),
                "model_qualifier": qualifier,
                "legs": {
                    "rule": "model_p - yes_ask - fee >= "
                            f"{config.HUNTER_MODEL_EDGE_MIN} (readout of "
                            "the existing shadow model; observational)",
                    "captured_at": captured_at.isoformat(),
                    "outcome_key": pc.outcome_key,
                    "model_probability": p,
                    "prediction_run_id": run.id,
                    "model_approval_decision_id":
                        run.model_approval_decision_id,
                    "yes_ask_dollars": _s(ask),
                    "fee_dollars": str(fee),
                    "net_edge_dollars": str(net_edge),
                    "fee_schedule": fees.block(),
                    "fee_policy": fees.version,
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


def _in_play_alert_eligible(f: dict) -> bool:
    """Whether an IN_PLAY_OVERREACTION may alert.

    DEFAULT FALSE, by config, and that default is the point. Richer
    conditioning evidence makes this finding more INTERESTING; it does not
    make promoting a context-class row to the channel a consenting third
    party reads an implementer's decision. Son deferred that call, so this
    is off until he turns it on, and when he does it rides the same
    AMBIENT_DETAIL / detail-channel path as everything else here — never
    the act-now channel, never the phone.

    Even switched ON, only the STRONGEST conditioning state qualifies:
      - `conditioning_observed` is refused, because an explaining event
        makes the move MORE reasonable. Alerting on a move that has a
        visible cause would broadcast the opposite of what the evidence
        says — this is the inference direction, enforced at the gate
        rather than only described in a docstring;
      - `conditioning_unobserved_no_stats` is refused, because "we could
        not look" is not a finding worth interrupting anyone for.
    """
    if not config.HUNTER_IN_PLAY_ALERTS_ENABLED:
        return False
    cond = (f.get("legs") or {}).get("conditioning") or {}
    return (cond.get("state")
            == apifootball_live.CONDITIONING_UNOBSERVED_STATS_AVAILABLE)


def _alert_eligible(f: dict) -> bool:
    """Whether a finding MAY alert: structural type, non-context, a fee
    schedule this repo actually models (P0-1), margin over the config
    bar, AND positive executable size proven on every required leg
    (P1-6). Eligibility is not delivery — the durable claim +
    confirmed-transport path below decides that.

    IN_PLAY_OVERREACTION is the one CONTEXT type with a route here, and
    only through `_in_play_alert_eligible`'s own config flag (default
    OFF). It carries no margin and no liquidity proof, so it is checked
    before the structural requirements below rather than being made to
    satisfy them."""
    if f["finding_type"] == "IN_PLAY_OVERREACTION":
        return _in_play_alert_eligible(f)
    if f["finding_type"] not in ALERTABLE or f.get("is_context"):
        return False
    # fee_unknown can never alert: a structural margin computed with the
    # wrong fee schedule is a false finding, and an unpriced one is not
    # a margin at all
    if f.get("fee_status") != "modeled":
        return False
    if f.get("net_margin_dollars") is None:
        return False
    if not _clocks_ordered(f):
        return False
    if Decimal(f["net_margin_dollars"]) < Decimal(
            str(config.HUNTER_ALERT_MIN_MARGIN_DOLLARS)):
        return False
    if f.get("alertable_liquidity") is not True:
        return False
    return True


def _alert_message(f: dict) -> str:
    subject = f.get("market_ticker") or f.get("event_ticker") or f["series"]
    legs = f.get("legs") or {}
    if f["finding_type"] == "IN_PLAY_OVERREACTION":
        # No margin and no executable size exist for this type, so it gets
        # its own copy rather than being forced through a template that
        # would print "net margin $None". The conditioning state is stated
        # explicitly, including that the feed showed no cause — a reader
        # must not have to infer which of the three states this is.
        cond = legs.get("conditioning") or {}
        xg = legs.get("live_xg_variant") or {}
        xg_bit = ""
        if xg.get("available"):
            xg_bit = (f" Live xG differential "
                      f"{xg.get('xg_differential_first_minus_second')} "
                      f"(characterisation only, not a reprice).")
        return (f"🔎 hunter observation [IN_PLAY_OVERREACTION] {subject}: "
                f"mid moved ${legs.get('mid_move_dollars')} between two "
                f"captures, exceeding both the threshold and the wider "
                f"spread. Conditioning: {cond.get('state')} — "
                f"{cond.get('reason')}.{xg_bit} The market-anchored move "
                f"is the primary signal and is model-free; an observed "
                f"cause would make the move MORE reasonable, not less. "
                f"({SHADOW_FRAMING}.)")
    margin = f["net_margin_dollars"]
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
    policy = f.get("fee_policy_version") or FEE_POLICY["version"]
    return (f"🔎 hunter observation [{f['finding_type']}] {subject}: "
            f"{detail}; net margin ${margin} per contract after exact "
            f"fees ({policy}); top-of-book executable up "
            f"to {f.get('limiting_quantity')} contracts. "
            f"({SHADOW_FRAMING}.)")


def _try_claim_alert(s, finding_id: int, now: datetime) -> bool:
    """Atomically claim the right to alert ONE finding — the DB row is
    the cross-process lock. UPDATE ... WHERE is atomic (PostgreSQL
    row-locks and re-evaluates the WHERE), so overlapping Railway
    containers cannot both win. The claim is COMMITTED before any bytes
    leave the process, and it is a LEASE (ALERT_CLAIM_LEASE_MINUTES):
    a process that dies after claiming leaves a claim that expires
    rather than muting the finding forever."""
    from sqlalchemy import or_
    cutoff = now - timedelta(minutes=ALERT_CLAIM_LEASE_MINUTES)
    n = (s.query(HunterFinding)
         .filter(HunterFinding.id == finding_id,
                 HunterFinding.status == "open",
                 HunterFinding.alerted_at.is_(None),
                 or_(HunterFinding.alert_claimed_at.is_(None),
                     HunterFinding.alert_claimed_at < cutoff))
         .update({HunterFinding.alert_claimed_at: now},
                 synchronize_session=False))
    s.commit()
    return n == 1


def _dispatch_alert(s, row: HunterFinding, f: dict) -> bool:
    """Deliver one observational alert with durable ordering (P0-2):

      1. budget check (per-process throttle; consumed only on success)
      2. durable CLAIM committed on the row — before any external send
      3. send; read PER-TRANSPORT results (src.alerts returns them)
      4. >=1 confirmed acceptance → alerted_at + budget consumed;
         otherwise the claim is RELEASED, the failure detail retained,
         and the still-open finding retries next cycle.

    A DB failure before step 2's commit means nothing was sent; a send
    that succeeds is preceded by a durable claim, so a crash afterwards
    can at worst delay the alerted_at mark until the lease expires —
    never mint unbounded duplicates."""
    now = _now()
    if not _alert_budget_ok(now):
        print(f"[hunter] alert suppressed (budget): {f['finding_type']} "
              f"{f.get('market_ticker') or f.get('event_ticker')}")
        return False
    if not _try_claim_alert(s, row.id, now):
        return False      # another process owns it, or it already alerted
    try:
        # AMBIENT_DETAIL on the DETAIL channel only — Son's decision,
        # 2026-07-29, and the narrowest step that answers what he asked
        # for. He wants to hear when something structural appears in the
        # 25-odd tradeable friendlies; the gate had been refusing these
        # entirely, so the scanner recorded everything and said nothing.
        #
        # Why this class rather than a new permissive one: AMBIENT_DETAIL
        # already means "may ride the quiet channel, never the act-now
        # channel and never the phone", and the gate ENFORCES that by
        # refusing any kind != "detail". So a structural finding reaches
        # the channel Son reads deliberately, and cannot reach the one
        # that interrupts him. Inventing a fourth class permitted on the
        # action channel would have been a real widening of the money
        # boundary; this is not that.
        #
        # What is deliberately NOT settled here: whether a mechanically
        # verifiable market fact — "these three asks sum to 96.8c net of
        # exact fees" — deserves its own class that the lock permits
        # outright. It is arithmetic on observed prices, not a forecast,
        # and the argument for it is real. Son deferred that question and
        # it stays deferred; see strategy/BACKLOG.md. If it is ever
        # granted, this call site is where it lands.
        #
        # The kind is pinned to "detail" here rather than passed in, so a
        # future caller cannot promote a finding to the phone by argument.
        from src.alerts import AMBIENT_DETAIL, send_alert
        results = send_alert(_alert_message(f), title="Trivela hunter",
                             kind="detail",
                             dispatch_class=AMBIENT_DETAIL)
        if not isinstance(results, dict):
            results = {"unknown_transport_contract": False}
    except Exception as exc:              # alerting must never break a scan
        results = {"dispatch_exception": str(exc)[:300]}
    accepted = any(v is True for v in results.values())
    prior = {}
    if row.alert_results_json:
        try:
            prior = json.loads(row.alert_results_json)
        except (ValueError, TypeError):
            prior = {}
    row.alert_results_json = json.dumps({
        "attempts": int(prior.get("attempts") or 0) + 1,
        "last": {"at": now.isoformat(), "results": results,
                 "accepted": accepted}}, sort_keys=True)
    if accepted:
        row.alerted_at = now
        _alert_times.append(now)          # budget consumed ONLY on success
    else:
        row.alert_claimed_at = None       # release → retryable next cycle
        print(f"[hunter] alert delivery failed (finding stays "
              f"retryable): {results}")
    try:
        s.commit()
    except Exception as exc:
        s.rollback()
        print(f"[hunter] alert bookkeeping commit failed: {exc}")
    return accepted


# --- persistence + the cycle ----------------------------------------------
def _finding_key(f: dict) -> str:
    """Provider-stable identity: type|series|event|market (P1-5). The
    partial unique index on open rows makes this a database invariant."""
    return "|".join([f["finding_type"], f["series"],
                     f.get("event_ticker") or "",
                     f.get("market_ticker") or ""])


def _row_key(row: HunterFinding) -> str:
    return "|".join([row.finding_type, row.series,
                     row.event_ticker or "", row.market_ticker or ""])


def _advance_watermark(s, key: str, clock: datetime) -> bool:
    """Claim `clock` as the newest observation basis for `key` (P0-3).

    Railway keeps the old container serving while the new one boots, so
    an OLDER scan legitimately commits AFTER a newer one. This is the
    reconciliation point, and the arbiter is the DATABASE: an atomic
    `UPDATE ... WHERE observed_through < :clock` re-evaluates its own
    predicate under a row lock, so exactly one writer can advance past
    any given instant. Returns False when this write is STALE — the
    caller must then ignore it entirely: no insert, no last_seen bump,
    no expiry, and no alert.

    A first observation of a key inserts the row under a UNIQUE
    constraint; a concurrent insert surfaces as an IntegrityError at
    flush and is handled exactly like the finding-row race — rollback
    and one retry pass, which then reads the winner's watermark."""
    n = (s.query(HunterObservationWatermark)
         .filter(HunterObservationWatermark.finding_key == key,
                 HunterObservationWatermark.observed_through < clock)
         .update({HunterObservationWatermark.observed_through: clock,
                  HunterObservationWatermark.updated_at: _now()},
                 synchronize_session=False))
    if n == 1:
        return True
    seen = (s.query(HunterObservationWatermark.id)
            .filter(HunterObservationWatermark.finding_key == key).first())
    if seen is not None:
        return False           # a same-or-newer observation already won
    s.add(HunterObservationWatermark(
        finding_key=key, observed_through=clock, updated_at=_now()))
    s.flush()
    return True


def _insert_finding_row(s, f: dict, key: str,
                        now: datetime) -> HunterFinding:
    """Construct + add one open finding under the partial unique index
    (one open row per finding_key — enforced by the DATABASE on both
    dialects). Conflict handling deliberately lives at COMMIT time in
    scan_cycle rather than in a per-row SAVEPOINT: stock pysqlite
    implicitly commits around SAVEPOINT statements (verified here —
    savepoint contents survived an outer rollback), so a savepoint-based
    path would be untestable on the SQLite suite and violate the test-DB
    parity rule. A plain add + one commit-retry behaves identically on
    both engines (P1-5)."""
    captured = f.get("captured_at") or now
    row = HunterFinding(
        competition_slug=f.get("competition_slug"),
        series=f["series"],
        event_ticker=f.get("event_ticker"),
        market_ticker=f.get("market_ticker"),
        finding_type=f["finding_type"],
        finding_key=key,
        is_context=bool(f.get("is_context")),
        legs_json=json.dumps(f["legs"], sort_keys=True),
        net_margin_dollars=f.get("net_margin_dollars"),
        # the schedule THIS finding's arithmetic used, resolved from the
        # provider's own series object — never a global assumption
        fee_policy_version=(f.get("fee_policy_version")
                            if not f.get("is_context") else None),
        model_qualifier_json=(
            json.dumps(f["model_qualifier"], sort_keys=True)
            if f.get("model_qualifier") else None),
        fixture_id=f.get("fixture_id"),
        first_captured_at=captured,      # the fetch-completion clock,
        last_seen_at=captured,           # never the persistence clock
        detected_at=f.get("detected_at"),
        persisted_at=now,
        observed_cycles=1,
        espn_captured_at=f.get("espn_captured_at"),
        status="open")
    s.add(row)
    return row


def scan_cycle() -> dict:
    """One full hunter pass: roster upkeep, per-series market fetch with
    per-series OUTCOME accounting, detection, append-only persistence
    with fail-closed expiry, durably-ordered alerts, and the
    heartbeat/denominator cycle row. Registered in the scheduler with
    coalesce + max_instances=1 (per-process; the cross-process guards
    are the partial unique index and the alert claim)."""
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
    discovery_error = None
    if discovery_due:
        roster = discover_roster(counter)
        if roster is not None:
            _roster["series"] = roster
            _roster["at"] = started
        else:
            discovery_error = (
                "roster discovery failed; scanning the STALE roster from "
                + (_roster["at"].isoformat() if _roster["at"] else "never"))
    series_all = list(_roster["series"])
    # between discoveries, only series that recently had open markets
    if discovery_due or not _roster["active"]:
        to_scan = series_all
    else:
        to_scan = [t for t in series_all if t in _roster["active"]]
    to_scan = to_scan[:MAX_SERIES_PER_CYCLE]

    events_seen = markets_seen = 0
    new_findings: list[dict] = []
    # per-event leg tickers: the ONLY defined way to read an event's two
    # team codes (the concatenated ticker cannot be split without already
    # knowing the answer; the legs carry them separately)
    event_market_tickers: dict[str, list[str]] = {}
    series_outcomes: dict[str, dict] = {}     # ticker -> {outcome, detail?}
    series_capture: dict[str, datetime] = {}  # fetch-completion clocks
    mls_markets_by_ticker: dict[str, dict] = {}
    active_now: set[str] = set()
    error: str | None = None

    try:
        _prune_pair_store(started)
        for series in to_scan:
            try:
                ms, truncated = _paged_markets(series, counter)
            except Exception as exc:
                # P0-1: a failed fetch is a RECORDED outcome, never a
                # silent skip — the series stays scheduled and its open
                # findings cannot expire this cycle
                print(f"[hunter] {series} fetch failed: {exc}")
                series_outcomes[series] = {
                    "outcome": SERIES_REQUEST_FAILED,
                    "detail": str(exc)[:300]}
                continue
            # the per-series capture clock: fetch COMPLETION time —
            # the only timing evidence these quotes get (P0-4)
            captured_at = _now()
            series_capture[series] = captured_at
            if ms:
                active_now.add(series)
            markets_seen += len(ms)
            outcome = {"outcome": SERIES_SUCCESS}
            if truncated:
                outcome = {
                    "outcome": SERIES_PAGINATION_CAP,
                    "detail": (f"cursor still live after "
                               f"{MAX_PAGES_PER_SERIES}x{PAGE_LIMIT} "
                               "markets; scope INCOMPLETE — unseen "
                               "markets cannot be declared anomaly-free")}
            # P0-4: provider-stable identity is a PRECONDITION, not a
            # nicety. A market missing its event ticker or its own
            # ticker cannot be grouped, keyed or de-duplicated, and the
            # old code swept those rows into a synthetic ""-event that
            # could produce a structural finding and alert. An identity
            # failure is a RECORDED per-series outcome that degrades the
            # cycle and yields zero findings and zero alerts from this
            # series — expiry blocked, series stays scheduled.
            bad = [m for m in ms
                   if not m.get("event_ticker") or not m.get("ticker")]
            if bad:
                sample = [{"ticker": m.get("ticker"),
                           "event_ticker": m.get("event_ticker")}
                          for m in bad[:3]]
                series_outcomes[series] = {
                    "outcome": SERIES_IDENTITY_FAILED,
                    "detail": (f"{len(bad)} of {len(ms)} markets lack a "
                               "provider-stable identity (event_ticker "
                               "and/or ticker); NO finding may be derived "
                               f"from this series' scan. sample={sample}"
                               )[:300]}
                print(f"[hunter] {series} identity failure: "
                      f"{len(bad)}/{len(ms)} markets unidentifiable")
                continue
            fees = _fee_schedule(series)
            try:
                by_event: dict[str, list[dict]] = {}
                for m in ms:
                    by_event.setdefault(m["event_ticker"], []).append(m)
                    if series == MLS_SERIES:
                        mls_markets_by_ticker[m["ticker"]] = m
                events_seen += len(by_event)
                for ev_ticker, ev_markets in by_event.items():
                    event_market_tickers[ev_ticker] = [
                        m.get("ticker") for m in ev_markets
                        if m.get("ticker")]
                    new_findings.extend(detect_event_findings(
                        series, ev_ticker, ev_markets, captured_at, fees))
                    # capture-paired repricing vs the PREVIOUS cycle's
                    # book, then refresh the pair store for the next
                    for m in ev_markets:
                        f = detect_overreaction(
                            series, ev_ticker, m,
                            _pair_store.get(m.get("ticker")), captured_at)
                        if f:
                            new_findings.append(f)
                        _update_pair_store(m, captured_at)
            except Exception as exc:
                # P0-1: a detector failure poisons only THIS series —
                # recorded, expiry-blocked, other series continue
                print(f"[hunter] {series} detection failed: {exc}")
                outcome = {"outcome": SERIES_DETECTION_FAILED,
                           "detail": str(exc)[:300]}
            series_outcomes[series] = outcome
    except Exception as exc:            # provider loop must not kill DB write
        error = f"scan: {exc}"
        print(f"[hunter] scan error: {exc}")

    def _refresh_active() -> set:
        # P0-1: a series leaves the schedule ONLY through a complete
        # successful scan that saw no open markets. Failed/truncated
        # series stay scheduled for the next cycle.
        failed = {t for t, o in series_outcomes.items()
                  if o["outcome"] != SERIES_SUCCESS}
        if series_outcomes:
            _roster["active"] = active_now | failed
        return failed

    failed_series = _refresh_active()

    s = get_session()
    created = expired = alerted = stale_rejected = 0
    alert_candidates: list[tuple[HunterFinding, dict]] = []
    live_stats_report: dict = {"ran": False, "candidates": 0,
                               "requests": 0,
                               "skipped": "the live-evidence pass did not "
                                          "reach its entry point"}
    status = "failed"
    try:
        if MLS_SERIES in series_capture:
            # the MLS quotes' own fetch-completion clock — never a
            # cycle-end clock (P0-4; a controlled scan measured 40s of
            # drift between the two)
            mls_captured = series_capture[MLS_SERIES]

            def _degrade_mls(detail: str):
                nonlocal failed_series
                series_outcomes[MLS_SERIES] = {
                    "outcome": SERIES_DETECTION_FAILED, "detail": detail}
                failed_series = _refresh_active()

            mls_fees = _fee_schedule(MLS_SERIES)
            try:
                pc_found, espn_complete = detect_post_certainty(
                    s, mls_markets_by_ticker, mls_captured, mls_fees,
                    counter)
                # P0-2 at the COMPOSED layer: a row whose quote clock
                # precedes the ESPN finality read is dropped here too,
                # and the drop degrades the pass rather than passing
                # silently
                ordered = [f for f in pc_found if _clocks_ordered(f)]
                new_findings.extend(ordered)
                if len(ordered) != len(pc_found):
                    espn_complete = False
                    print("[hunter] dropped POST_CERTAINTY rows whose "
                          "quote clock preceded the ESPN finality read")
                if not espn_complete:
                    _degrade_mls("ESPN re-read / post-finality re-quote "
                                 "incomplete: the POST_CERTAINTY pass did "
                                 "not cover every candidate — expiry "
                                 "blocked")
            except Exception as exc:
                print(f"[hunter] post-certainty error: {exc}")
                _degrade_mls(f"post-certainty: {exc}"[:300])
            try:
                new_findings.extend(detect_model_edge(
                    s, mls_markets_by_ticker, mls_captured, mls_fees))
            except Exception as exc:
                print(f"[hunter] model-edge error: {exc}")
                _degrade_mls(f"model-edge: {exc}"[:300])

        # LIVE-EVIDENCE PASS — make the conditioning event observable where
        # coverage allows. Deliberately AFTER detection and BEFORE
        # persistence, so the conditioning state is part of the row that
        # gets committed rather than a later mutation of stored evidence.
        # It must never break a scan: a provider failure leaves every
        # finding in the honest conditioning_unobserved_no_stats state.
        try:
            live_stats_report = enrich_overreactions(
                s, new_findings, event_market_tickers, started)
        except Exception as exc:
            live_stats_report = {
                "error": apifootball_live.redact(
                    f"{type(exc).__name__}: {exc}")}
            print(f"[hunter] live-evidence pass failed: "
                  f"{type(exc).__name__}")
        counter["live_stats_requests"] = live_stats_report.get(
            "requests", 0) or 0

        status = ("failed" if error
                  else "degraded" if failed_series or discovery_error
                  else "complete")

        def _persist_once():
            """One transactional persistence pass: expiry + upsert +
            cycle row, committed together. Runs at most twice — a
            cross-process duplicate insert (P1-5) surfaces at COMMIT as
            the partial unique index rejecting the loser; the retry
            re-reads open rows, adopts the winner's, and never counts
            the race as a new finding."""
            nonlocal created, expired, stale_rejected
            created = expired = stale_rejected = 0
            alert_candidates.clear()
            now = _now()
            # P0-1: expiry ONLY after a COMPLETE successful detection
            # pass for the finding's series in THIS cycle. "Didn't
            # look" is never recorded as "no anomaly".
            expirable_series = {t for t, o in series_outcomes.items()
                                if o["outcome"] == SERIES_SUCCESS}
            current_keys = {_finding_key(f) for f in new_findings}
            open_rows = (s.query(HunterFinding)
                         .filter_by(status="open").all())
            open_by_key = {}
            for row in open_rows:
                open_by_key[_row_key(row)] = row
                if row.series in expirable_series \
                        and _row_key(row) not in current_keys:
                    # P0-3: expiring is an OBSERVATION too, made at this
                    # series' capture clock — never the persistence
                    # clock, and never applied if a newer process has
                    # already observed this key
                    at = series_capture.get(row.series) or now
                    if not _advance_watermark(s, _row_key(row), at):
                        stale_rejected += 1
                        continue
                    row.status = "expired"
                    row.expired_at = now
                    expired += 1
            for f in new_findings:
                key = _finding_key(f)
                cap = f.get("captured_at") or now
                # P0-3: every write is gated on the observation clock.
                # An older container's anomaly arriving after a newer
                # clean scan loses HERE, at the database, so it can
                # neither re-open the finding nor alert on it.
                if not _advance_watermark(s, key, cap):
                    stale_rejected += 1
                    print(f"[hunter] stale observation ignored "
                          f"(clock {cap.isoformat()} not newer than the "
                          f"recorded basis): {key}")
                    continue
                existing = open_by_key.get(key)
                if existing is not None and existing.status == "open":
                    existing.last_seen_at = cap
                    existing.observed_cycles = (existing.observed_cycles
                                                or 1) + 1
                    # retryable: an open finding that never achieved a
                    # confirmed delivery may try again (P0-2)
                    if existing.alerted_at is None \
                            and _alert_eligible(f):
                        alert_candidates.append((existing, f))
                    continue
                row = _insert_finding_row(s, f, key, now)
                created += 1
                open_by_key[key] = row
                if _alert_eligible(f):
                    alert_candidates.append((row, f))
            s.add(HunterCycle(
                started_at=started, completed_at=_now(), status=status,
                series_scanned=len(series_capture),
                series_failed=len(failed_series),
                series_outcomes_json=json.dumps(series_outcomes,
                                                sort_keys=True),
                events_scanned=events_seen,
                markets_scanned=markets_seen,
                findings_new=created, findings_expired=expired,
                stale_writes_rejected=stale_rejected,
                request_count=counter.get("requests", 0),
                # the SECOND provider's spend, kept in its own column: one
                # combined number would hide which provider a budget
                # problem came from, and they have different limits
                live_stats_request_count=counter.get(
                    "live_stats_requests", 0),
                live_stats_json=json.dumps(live_stats_report,
                                           sort_keys=True, default=str),
                roster_size=len(series_all),
                active_series=len(active_now),
                discovery_at=_roster["at"],
                discovery_error=discovery_error,
                error=error))
            # durable BEFORE any alert leaves the process (P0-2): a
            # failure here means nothing was sent and nothing claims
            # to have been
            s.commit()

        from sqlalchemy.exc import IntegrityError
        try:
            _persist_once()
        except IntegrityError as exc:
            # the overlapping container won an insert between our read
            # and our commit — adopt its rows on a single retry
            print(f"[hunter] persist conflict (cross-process race), "
                  f"retrying once: {exc}")
            s.rollback()
            _persist_once()
    except Exception as exc:
        s.rollback()
        print(f"[hunter] persist failed: {exc}")
        # the heartbeat must record the failure — a dead scanner must be
        # visible as dead, not as a quiet market
        try:
            s.add(HunterCycle(started_at=started, completed_at=_now(),
                              status="failed",
                              series_scanned=len(series_capture),
                              series_failed=len(failed_series),
                              series_outcomes_json=json.dumps(
                                  series_outcomes, sort_keys=True),
                              discovery_at=_roster["at"],
                              discovery_error=discovery_error,
                              error=f"persist: {exc}"[:2000]))
            s.commit()
        except Exception:
            s.rollback()
        s.close()
        return {"error": str(exc)[:200], "status": "failed"}

    # dispatch phase — strictly AFTER the durable commit above; each
    # delivery outcome commits its own bookkeeping (P0-2)
    try:
        for row, f in alert_candidates:
            if _dispatch_alert(s, row, f):
                alerted += 1
    except Exception as exc:
        print(f"[hunter] alert dispatch failed: {exc}")
    finally:
        s.close()
    return {"status": status,
            "series_scanned": len(series_capture),
            "series_failed": len(failed_series),
            "events": events_seen, "markets": markets_seen,
            "findings_new": created, "findings_expired": expired,
            "stale_writes_rejected": stale_rejected,
            "alerted": alerted,
            "kalshi_requests": counter.get("requests", 0),
            "live_stats": live_stats_report,
            "error": error}


# --- the public read surface ----------------------------------------------
def findings_report(competition: str | None = None,
                    finding_type: str | None = None,
                    status: str | None = None,
                    limit: int = 100) -> dict:
    """Findings WITH their denominators. A count without cycles-run /
    markets-scanned is a defect in this repo; the heartbeat age makes a
    dead scanner distinguishable from a quiet market, and the last
    cycle's per-series outcomes make an INCOMPLETE scan distinguishable
    from a clean one (P0-1)."""
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
        by_status = {st: n for st, n in
                     s.query(HunterCycle.status,
                             func.count(HunterCycle.id))
                     .group_by(HunterCycle.status).all()}
        markets_total = s.query(
            func.coalesce(func.sum(HunterCycle.markets_scanned), 0)).scalar()
        last = (s.query(HunterCycle)
                .order_by(HunterCycle.id.desc()).first())
        last_age = None
        if last and last.completed_at:
            done = last.completed_at
            done = done if done.tzinfo else done.replace(tzinfo=timezone.utc)
            last_age = int((_now() - done).total_seconds())
        incomplete = None
        if last and last.series_outcomes_json:
            try:
                incomplete = {t: o for t, o in json.loads(
                    last.series_outcomes_json).items()
                    if o.get("outcome") != SERIES_SUCCESS} or None
            except (ValueError, TypeError):
                incomplete = None

        qual, model_status = _model_state()
        return {
            "ready": True,
            "framing": SHADOW_FRAMING,
            "capture_clock": CAPTURE_CLOCK_NOTE,
            "fee_policy": FEE_POLICY["version"],
            "fee_policy_note": FEE_RESOLUTION_NOTE,
            "denominators": {
                "cycles_run": cycles,
                "cycles_by_status": {
                    "complete": by_status.get("complete", 0),
                    "degraded": by_status.get("degraded", 0),
                    "failed": by_status.get("failed", 0),
                },
                "markets_scanned_total": int(markets_total or 0),
                "last_cycle": None if last is None else {
                    "status": last.status,
                    "completed_at": (last.completed_at.isoformat()
                                     if last.completed_at else None),
                    "age_seconds": last_age,
                    "series_scanned": last.series_scanned,
                    "series_failed": last.series_failed,
                    # writes a NEWER process had already superseded,
                    # rejected at the observation watermark
                    "stale_writes_rejected": last.stale_writes_rejected,
                    # non-SUCCESS series with reasons: what the cycle
                    # did NOT fully look at ("didn't look" is never
                    # served as "no anomaly")
                    "incomplete_series": incomplete,
                    "markets_scanned": last.markets_scanned,
                    "roster_size": last.roster_size,
                    "active_series": last.active_series,
                    "discovery_at": (last.discovery_at.isoformat()
                                     if last.discovery_at else None),
                    "discovery_error": last.discovery_error,
                    "error": last.error,
                },
                "heartbeat_note": ("age_seconds far above the poll cadence "
                                   "means the scanner is DEAD, not that "
                                   "the market is quiet"),
                "roster_note": ("between discoveries only recently-active "
                                "series are scanned: a series going "
                                "active is seen with up to "
                                f"{config.HUNTER_DISCOVERY_MINUTES} "
                                "minutes of lag"),
            },
            "findings_per_type": per_type,
            "findings_per_type_scope": ("global — totals across ALL "
                                        "findings, NOT filtered by this "
                                        "query's parameters"),
            "model_status": {
                "mls-2026": (qual if qual is not None else model_status),
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
                "detected_at": (r.detected_at.isoformat()
                                if r.detected_at else None),
                "persisted_at": (r.persisted_at.isoformat()
                                 if r.persisted_at else None),
                "espn_captured_at": (r.espn_captured_at.isoformat()
                                     if r.espn_captured_at else None),
                "status": r.status,
                "expired_at": (r.expired_at.isoformat()
                               if r.expired_at else None),
                "alerted_at": (r.alerted_at.isoformat()
                               if r.alerted_at else None),
                "alert_results": (json.loads(r.alert_results_json)
                                  if r.alert_results_json else None),
            } for r in rows],
        }
    finally:
        s.close()
