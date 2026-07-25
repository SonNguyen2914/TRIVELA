"""Kalshi market discovery, approved-alias mapping, and full-book
capture for the live plane (launch decision O6).

Mapping rule: an event attaches to a fixture ONLY when (a) the ticker's
ET-date segment matches the fixture's kickoff date and (b) BOTH title
sides resolve through APPROVED kalshi aliases to the fixture's teams.
Anything else stays unmapped and is reported by readiness. Quotes are
integer cents, both sides with sizes, plus order-book depth, each tied
to a content-hashed source observation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone

from decimal import Decimal

import requests

from src.live import identity
import config
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, MarketContract, MarketDepthLevel,
                             MarketEvent, MarketQuote, MarketSnapshot,
                             SourceObservation)

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXMLSGAME"

# every per-match family (Jul 23 discovery: 17 MLS series, 12 per-match).
# GAME comes first: it anchors the fixture mapping via approved aliases,
# and every other family suffix-joins to it in the same sweep.
FAMILY_SERIES = (
    "KXMLSGAME", "KXMLSTOTAL", "KXMLSBTTS", "KXMLSSPREAD",
    "KXMLSTEAMTOTAL", "KXMLSSCORE", "KXMLSFTTS", "KXMLSMOV",
    "KXMLS1H", "KXMLS1HTOTAL", "KXMLS1HSPREAD", "KXMLS1HBTTS",
)


def _now():
    return datetime.now(timezone.utc)


def _cents(m: dict, field: str) -> int | None:
    """Kalshi's native integer-cent field first (fixed point, per the
    decision); the *_dollars string only as fallback."""
    v = m.get(field)
    if isinstance(v, int):
        return v
    try:
        return int(round(float(m.get(f"{field}_dollars")) * 100))
    except (TypeError, ValueError):
        return None


def _ticker_date(event_ticker: str) -> str | None:
    m = re.match(rf"{SERIES}-(\d{{2}}[A-Z]{{3}}\d{{2}})", event_ticker or "")
    return m.group(1) if m else None


def _ticker_date_any(event_ticker: str) -> str | None:
    """Date segment for ANY family's event ticker."""
    m = re.match(r"^KXMLS[A-Z0-9]*-(\d{2}[A-Z]{3}\d{2})",
                 event_ticker or "")
    return m.group(1) if m else None


def _ticker_day(tdate: str | None) -> date | None:
    """'26JUL25' -> date(2026, 7, 25)."""
    if not tdate:
        return None
    try:
        return datetime.strptime(tdate, "%y%b%d").date()
    except ValueError:
        return None


# Kalshi rate-limits bursts hard (429/503 seen live Jul 23 on a tight
# contract-fetch loop). Every market-list request goes through this.
_MIN_GAP_S = 0.25
_last_call = 0.0


def _kalshi_get(url: str, **kw):
    global _last_call
    wait = _MIN_GAP_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    r = requests.get(url, timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def _kalshi_paged(url: str, params: dict, key: str,
                  page_limit: int = 200, max_pages: int = 30,
                  meta: dict | None = None) -> list:
    """Cursor-complete list retrieval (V8 eval F6: single-page calls
    silently truncated events and markets). Pages until the provider's
    cursor is exhausted or the sanity cap trips. When `meta` is supplied
    it is filled with {'pages', 'complete', 'cap_reached'} so a caller can
    record — and refuse to trust as complete — a registry that hit the cap
    rather than exhausting the cursor (V9 eval F6): a cap must produce an
    explicit incomplete state, never a silent truncation."""
    out: list = []
    cursor = None
    pages = 0
    for _ in range(max_pages):
        p = dict(params, limit=page_limit)
        if cursor:
            p["cursor"] = cursor
        d = _kalshi_get(url, params=p)
        out.extend(d.get(key) or [])
        pages += 1
        cursor = d.get("cursor")
        if not cursor:
            break
    cap_reached = bool(cursor)      # cursor still set == stopped at the cap
    if meta is not None:
        meta.update({"pages": pages, "complete": not cap_reached,
                     "cap_reached": cap_reached})
    return out


# --- current provider schema (verified live Jul 23 vs api docs) -----------
# Prices arrive as integer cents AND/OR "*_dollars" strings; sizes,
# volume and open interest as "*_fp" fixed-point strings; the order
# book under "orderbook_fp" with dollar-string [price, size] pairs.
PROVIDER_SCHEMA_VERSION = "kalshi-2026-07-fp"

# The lock-completeness predicate is versioned so "full book" cannot
# change meaning silently as families are added (V8.1 eval qual #3).
# v1: required = GAME 3-way; all other families captured-when-present.
# v2 (Jul 25 audit): freshness is measured on OUR CAPTURE CLOCK, not
# Kalshi's `updated_time` — see QUOTE_FRESHNESS_NOTE.
LOCK_POLICY_VERSION = "mls-lock-v2"

# Why execution freshness is capture-based (audit, Jul 25 2026).
#
# V9.1 eval F6 required a PROVIDER timestamp for execution-readiness, so
# that "fresh" could never be inferred from our own clock. Faithful — but
# it assumed Kalshi publishes a quote-update time. It does not. Measured
# on the live book: KXMLSGAME-26JUL25MINVAN-VAN, status ACTIVE, real
# two-sided prices (0.26/0.25), volume ~43k, open interest ~39k, and
#     updated_time = 2026-07-23T19:02:29Z  ->  ~108,880 s (~30 h) old
# Every KXMLSGAME market showed the same ~30 h, and no other time field
# exists (no last_trade_time). `updated_time` tracks the market
# DEFINITION, not the order book.
#
# The consequence was silent: `game_oldest_age <= 600` could never pass,
# so execution_ready was permanently False, EVERY paper signal was
# rejected NOT_EXECUTION_READY, and every fixture reported
# EXECUTION_NOT_READY — a gate that can never pass protects nothing, it
# only disables the evidence collection it was meant to guard.
#
# So freshness is now the age of OUR fetch (the book was pulled seconds
# before the lock), recorded honestly as basis 'capture_time'. This does
# NOT claim to know when the book last changed — no venue field permits
# that claim — and each quote still stores Kalshi's provider_timestamp so
# an auditor can see exactly how stale that field is.
QUOTE_FRESHNESS_NOTE = (
    "Kalshi publishes no quote-update timestamp (updated_time tracks the "
    "market definition and ran ~30h stale on active, two-sided markets), "
    "so execution freshness is measured on our capture clock and labelled "
    "'capture_time'. Provider timestamps are retained per quote for audit.")


def _fp_int(m: dict, field: str) -> int | None:
    """Fixed-point count field: prefer '{field}_fp' string, fall back
    to the legacy integer name."""
    v = m.get(f"{field}_fp")
    if v is not None:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            pass
    v = m.get(field)
    return v if isinstance(v, int) else None


def _dollars_str(m: dict, field: str) -> str | None:
    """The EXACT provider price string for a field (V9 eval F7): the
    '{field}_dollars' subpenny string when present, else the integer-cent
    value rendered as a dollar string. Retained beside the derived cents
    so subpenny prices are never rounded away at ingest."""
    v = m.get(f"{field}_dollars")
    if v is not None:
        return str(v)
    c = m.get(field)
    if isinstance(c, int):
        return f"{c / 100.0:.4f}"
    return None


def _sizes_fp(m: dict) -> str | None:
    """The EXACT provider size strings, by field, as one JSON blob (V9
    eval F7): fractional '{field}_fp' contract counts preserved beside the
    truncated integers."""
    fields = ("yes_bid_size", "yes_ask_size", "no_bid_size", "no_ask_size",
              "volume", "open_interest")
    out: dict[str, str] = {}
    for f in fields:
        v = m.get(f"{f}_fp")
        if v is None:
            v = m.get(f)
        if v is not None:
            out[f] = str(v)
    return json.dumps(out, sort_keys=True) if out else None


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rules_hash(m: dict) -> str | None:
    raw = (m.get("rules_primary") or "") + (m.get("rules_secondary") or "")
    return hashlib.sha256(raw.encode()).hexdigest() if raw else None


DEPTH_KEEP = 10          # levels retained per side (policy: best_10_each_side)


def _depth_levels(ob_payload: dict, keep: int = DEPTH_KEEP
                  ) -> list[tuple[str, int, int, str | None, str | None]]:
    """(side, price_c, size, price_dollars, size_fp) rows from the CURRENT
    'orderbook_fp' shape (dollar-string pairs), with the legacy
    integer-cent 'orderbook' shape as fallback. The derived cent/size
    ints stay a display comparator; the EXACT provider strings are
    retained beside them (V9 eval F7) and are what paper execution walks.

    We keep the BEST `keep` levels per side — the HIGHEST-priced bids
    (V9.1 eval F1). Kalshi returns each side's bids in ASCENDING price
    order (the best/highest bid is LAST), so the previous `[:10]` kept the
    WORST ten and dropped the best — for a NO bid at price q (= a YES ask
    at 1-q) that meant the executable top of book was missing. We now sort
    explicitly and keep the top by price, so the result is correct
    regardless of the provider's array order. The V8 evaluation proved the
    old parser stored ZERO depth rows against live responses."""
    rows: list[tuple[str, int, int, str | None, str | None]] = []
    fp = ob_payload.get("orderbook_fp")
    if isinstance(fp, dict):
        for side, key in (("yes", "yes_dollars"), ("no", "no_dollars")):
            parsed: list[tuple[Decimal, int, int, str, str]] = []
            for lvl in (fp.get(key) or []):
                try:
                    exact = Decimal(str(lvl[0]))
                    parsed.append((exact, int(round(float(lvl[0]) * 100)),
                                   int(float(lvl[1])),
                                   str(lvl[0]), str(lvl[1])))
                except (ArithmeticError, TypeError, ValueError, IndexError):
                    continue
            # best bids = highest price; keep top `keep` after an explicit
            # sort (never trust the provider's array order).
            #
            # Sort on the EXACT Decimal, never the derived cent (V9.3 eval
            # F1): Kalshi's fixed-point fields carry subpenny prices and
            # finer price grids are being rolled out, so many levels can
            # share one rounded cent. Sorting on the rounded value left
            # ties in provider order, which kept the FIRST ten and dropped
            # the true best — e.g. 0.5300..0.5311 all round to 53c and the
            # real best bid 0.5311 was discarded. Display cents are derived
            # only AFTER the levels are chosen.
            parsed.sort(key=lambda x: x[0], reverse=True)
            for _exact, price_c, size, pd, sf in parsed[:keep]:
                rows.append((side, price_c, size, pd, sf))
        return rows
    legacy = ob_payload.get("orderbook")
    if isinstance(legacy, dict):
        for side in ("yes", "no"):
            parsed_legacy: list[tuple[int, int]] = []
            for lvl in (legacy.get(side) or []):
                try:
                    parsed_legacy.append((int(lvl[0]), int(lvl[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            parsed_legacy.sort(key=lambda x: x[0], reverse=True)
            for price_c, size in parsed_legacy[:keep]:
                rows.append((side, price_c, size, None, None))
    return rows


def _fixture_et_date(dt) -> str:
    """Kalshi ticker dates are US-Eastern WALL CLOCK — a fixed UTC-4
    misdates late-evening fixtures after DST ends Nov 1 (V8 eval F11)."""
    from zoneinfo import ZoneInfo
    et = dt.astimezone(ZoneInfo("America/New_York"))
    return et.strftime("%y%b%d").upper()


def _try_map(s, row: MarketEvent, tdate: str | None) -> bool:
    """Attach an event to its fixture via the approved-alias rule.
    Retried on later sweeps: an event can arrive before its fixture, or
    before an alias fix lands."""
    title = row.title or ""
    if " vs " not in title or not tdate:
        return False
    k_home, k_away = title.split(" vs ", 1)
    home = identity.resolve("kalshi", k_home.strip())
    away = identity.resolve("kalshi", k_away.strip())
    if not (home and away):
        return False
    for f in (s.query(Fixture)
              .filter_by(competition_slug="mls-2026",
                         home_team_id=home.id,
                         away_team_id=away.id).all()):
        if (f.current_kickoff_utc and _fixture_et_date(
                f.current_kickoff_utc.replace(
                    tzinfo=f.current_kickoff_utc.tzinfo
                    or timezone.utc)) == tdate):
            row.fixture_id = f.id
            row.mapped_via = "alias"
            row.mapping_approved = True
            return True
    return False


def _ensure_contracts(s, row: MarketEvent) -> None:
    """Create contract rows for an event, and REPAIR existing rows whose
    outcome_key is still NULL — an event discovered before its fixture
    existed got label-only contracts, and they must heal once the
    mapping lands (seen live Jul 23: only 'Tie' resolvable pre-mapping).

    outcome keys: the GAME family resolves its side labels through the
    APPROVED alias table; every other family's key is parsed from the
    machine-readable ticker tail (src.mls.model_key_for) — no label
    guessing anywhere. Throttled; failures retry next sweep."""
    from src.mls import model_key_for
    # cursor-complete: a single limited page silently dropped contracts
    # from the registry, understating a lock snapshot's expected count
    # against an already-truncated universe (V9 eval F6)
    markets_list = _kalshi_paged(
        f"{KALSHI}/markets",
        {"event_ticker": row.kalshi_event_ticker}, "markets")
    fx = s.get(Fixture, row.fixture_id) if row.fixture_id else None
    suffix_codes = ""
    if "-" in (row.kalshi_event_ticker or ""):
        suffix_codes = row.kalshi_event_ticker.split("-", 1)[1][7:]
    for m in markets_list:
        label = (m.get("yes_sub_title") or m.get("title") or "").strip()
        okey = None
        if row.series == SERIES:
            if label.lower() == "tie":
                okey = "draw"
            else:
                t = identity.resolve("kalshi", label)
                if t and fx:
                    if t.id == fx.home_team_id:
                        okey = "home_win"
                    elif t.id == fx.away_team_id:
                        okey = "away_win"
        else:
            okey = model_key_for(row.series, m.get("ticker", ""),
                                 suffix_codes)
        existing = s.query(MarketContract).filter_by(
            ticker=m.get("ticker")).first()
        if existing is None:
            s.add(MarketContract(market_event_id=row.id,
                                 ticker=m.get("ticker"),
                                 side_label=label, outcome_key=okey))
        elif existing.outcome_key is None and okey:
            existing.outcome_key = okey


def _ticker_suffix(event_ticker: str) -> str | None:
    return (event_ticker.split("-", 1)[1]
            if "-" in (event_ticker or "") else None)


def discover_and_map() -> dict:
    """Sweep EVERY per-match family. GAME events attach to fixtures via
    the approved-alias rule; all other families share the game event's
    ticker suffix ({date}{HOME}{AWAY}), so they inherit its fixture by
    exact suffix join — no name resolution at all. Contract rows exist
    (and heal) for every current or future event; historical events are
    recorded without contract fetches (rate budget goes to the slate)."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    seen = mapped = unmapped = contracts_filled = 0
    truncated_series: list[str] = []
    horizon_floor = (_now() - timedelta(days=1)).date()
    try:
        for series in FAMILY_SERIES:
            try:
                # cursor-complete event discovery: a single 100-row page
                # truncated the registry once a series had >100 open
                # events, and the shortfall was silent (V9 eval F6)
                meta: dict = {}
                events = _kalshi_paged(
                    f"{KALSHI}/events", {"series_ticker": series},
                    "events", meta=meta)
                if not meta.get("complete", True):
                    truncated_series.append(series)
                    print(f"[markets] discovery {series} TRUNCATED at cap "
                          f"({meta.get('pages')} pages) — registry "
                          f"INCOMPLETE, not a clean full sweep")
            except requests.RequestException as exc:
                print(f"[markets] discovery {series} failed: {exc}")
                continue
            seen += len(events)
            for ev in events:
                ticker = ev.get("event_ticker")
                if not ticker:
                    continue
                tdate = _ticker_date_any(ticker)
                row = s.query(MarketEvent).filter_by(
                    kalshi_event_ticker=ticker).first()
                if row is None:
                    row = MarketEvent(competition_slug="mls-2026",
                                      kalshi_event_ticker=ticker,
                                      series=series,
                                      title=ev.get("title") or "",
                                      settlement_scope=(
                                          "first_half"
                                          if series.startswith("KXMLS1H")
                                          else "regular_time"),
                                      mapping_approved=False)
                    s.add(row)
                if not row.mapping_approved:
                    ok = False
                    if series == SERIES:
                        ok = _try_map(s, row, tdate)
                    else:
                        suffix = _ticker_suffix(ticker)
                        game = s.query(MarketEvent).filter_by(
                            kalshi_event_ticker=f"{SERIES}-{suffix}"
                        ).first() if suffix else None
                        if game is not None and game.fixture_id:
                            row.fixture_id = game.fixture_id
                            row.mapped_via = "suffix"
                            row.mapping_approved = True
                            ok = True
                    mapped += int(ok)
                    unmapped += int(not ok)
                s.flush()
                day = _ticker_day(tdate)
                existing = s.query(MarketContract).filter_by(
                    market_event_id=row.id).all()
                needs = (not existing
                         or (row.fixture_id is not None
                             and any(c.outcome_key is None
                                     for c in existing)))
                if day and day >= horizon_floor and needs:
                    try:
                        _ensure_contracts(s, row)
                        contracts_filled += 1
                    except requests.RequestException as exc:
                        print(f"[markets] contracts {ticker}: {exc}")
        # persist the sweep's completeness as a durable, auditable record
        # (V9.1 eval F10) — not just a transient return value
        from src.live.models import RegistryDiscovery
        s.add(RegistryDiscovery(
            competition_slug="mls-2026", provider="kalshi",
            complete=not truncated_series,
            truncated_series_json=json.dumps(truncated_series),
            events_seen=seen, newly_mapped=mapped, unmapped=unmapped,
            contracts_filled=contracts_filled, completed_at=_now()))
        s.commit()
        return {"events_seen": seen, "newly_mapped": mapped,
                "unmapped": unmapped,
                "contracts_filled": contracts_filled,
                "discovery_complete": not truncated_series,
                "truncated_series": truncated_series}
    except Exception as exc:
        s.rollback()
        print(f"[markets] mapping failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def _mapped_events_for(s, fixture_id: int | None,
                       horizon_hours: float) -> list:
    q = (s.query(MarketEvent)
         .filter_by(competition_slug="mls-2026", mapping_approved=True)
         .filter(MarketEvent.fixture_id.isnot(None)))
    events = []
    for me in q.all():
        if fixture_id is not None and me.fixture_id != fixture_id:
            continue
        fx = s.get(Fixture, me.fixture_id)
        if fx is None or fx.current_kickoff_utc is None:
            continue
        ko = fx.current_kickoff_utc
        ko = ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)
        if fixture_id is None and not (
                -3 <= (ko - _now()).total_seconds() / 3600
                <= horizon_hours):
            continue
        events.append(me)
    return events


def _quote_row(m: dict, mc_id: int, obs_id: int,
               snapshot_id: int | None = None) -> MarketQuote:
    """One market payload -> a MarketQuote on the CURRENT provider
    schema (prices in cents/dollars, sizes/volume/OI as *_fp strings,
    provider timestamp, rules hash) with legacy fallbacks."""
    return MarketQuote(
        market_contract_id=mc_id, captured_at=_now(),
        market_snapshot_id=snapshot_id,
        provider_timestamp=_parse_ts(m.get("updated_time")),
        yes_bid_c=_cents(m, "yes_bid"), yes_ask_c=_cents(m, "yes_ask"),
        no_bid_c=_cents(m, "no_bid"), no_ask_c=_cents(m, "no_ask"),
        yes_bid_size=_fp_int(m, "yes_bid_size"),
        yes_ask_size=_fp_int(m, "yes_ask_size"),
        no_bid_size=_fp_int(m, "no_bid_size"),
        no_ask_size=_fp_int(m, "no_ask_size"),
        last_trade_c=_cents(m, "last_price"),
        volume=_fp_int(m, "volume"),
        open_interest=_fp_int(m, "open_interest"),
        status=m.get("status"), rules_hash=_rules_hash(m),
        fee_schedule_version=m.get("fee_type"),
        # exact provider values retained beside the derived cents (F7)
        yes_bid_dollars=_dollars_str(m, "yes_bid"),
        yes_ask_dollars=_dollars_str(m, "yes_ask"),
        no_bid_dollars=_dollars_str(m, "no_bid"),
        no_ask_dollars=_dollars_str(m, "no_ask"),
        sizes_fp_json=_sizes_fp(m),
        provider_precision=PROVIDER_SCHEMA_VERSION)


def _fetch_event_books(events) -> tuple[dict, dict, list[str]]:
    """External I/O only (no session held): every event's markets,
    cursor-complete, plus each market's order book. Returns
    (markets_by_event, orderbook_by_ticker, failed_event_tickers)."""
    markets_by_event: dict[str, list[dict]] = {}
    books: dict[str, dict] = {}
    failed: list[str] = []
    for me in events:
        try:
            ms = _kalshi_paged(f"{KALSHI}/markets",
                               {"event_ticker": me.kalshi_event_ticker},
                               "markets")
            markets_by_event[me.kalshi_event_ticker] = ms
            for m in ms:
                try:
                    books[m.get("ticker")] = _kalshi_get(
                        f"{KALSHI}/markets/{m.get('ticker')}/orderbook")
                except requests.RequestException:
                    pass                     # depth best-effort
        except requests.RequestException as exc:
            print(f"[markets] fetch {me.kalshi_event_ticker}: {exc}")
            failed.append(me.kalshi_event_ticker)
    return markets_by_event, books, failed


def capture_quotes(fixture_id: int | None = None,
                   horizon_hours: float = 48.0) -> dict:
    """Routine observation stream: quotes + depth for mapped events in
    the horizon. NOT the lock path — lock evidence goes through
    capture_lock_snapshot, which validates completeness."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    quotes = 0
    try:
        events = _mapped_events_for(s, fixture_id, horizon_hours)
        markets_by_event, books, _ = _fetch_event_books(events)
        for me in events:
            ms = markets_by_event.get(me.kalshi_event_ticker)
            if ms is None:
                continue
            raw = json.dumps(ms, sort_keys=True)
            obs = SourceObservation(
                source="kalshi",
                endpoint=f"markets?event={me.kalshi_event_ticker}",
                content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                payload_json=raw[:config.OBSERVATION_PAYLOAD_MAX_BYTES],
                observed_at=_now())
            s.add(obs)
            s.flush()
            for m in ms:
                mc = s.query(MarketContract).filter_by(
                    ticker=m.get("ticker")).first()
                if mc is None:
                    continue
                quote = _quote_row(m, mc.id, obs.id)
                quote.source_observation_id = obs.id
                s.add(quote)
                s.flush()
                for side, price_c, size, price_d, size_fp in _depth_levels(
                        books.get(m.get("ticker")) or {}):
                    s.add(MarketDepthLevel(
                        market_quote_id=quote.id, side=side,
                        price_c=price_c, size=size,
                        price_dollars=price_d, size_fp=size_fp))
                quotes += 1
        s.commit()
        return {"events": len(events), "quotes": quotes}
    except Exception as exc:
        s.rollback()
        print(f"[markets] capture failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def capture_lock_snapshot(fixture_id: int) -> dict | None:
    """The lock-grade capture (V8 evaluation F1): fetch EVERYTHING
    externally first, then one transaction writing observations, a
    MarketSnapshot header, and linked quotes; the snapshot completes
    only when every mapped event fetched and the game family's three
    quotes are present. Returns {'snapshot_id', 'quote_by_ticker'} on
    success; on ANY shortfall records a failed snapshot and returns
    None — the caller must then NOT create a canonical lock."""
    if not plane_ready():
        return None
    s = get_session()
    try:
        events = _mapped_events_for(s, fixture_id, 0)
        if not events:
            print(f"[markets] lock snapshot: no mapped events "
                  f"for fixture {fixture_id}")
            return None
        contracts_expected = sum(
            s.query(MarketContract).filter_by(market_event_id=me.id).count()
            for me in events)
        markets_by_event, books, failed = _fetch_event_books(events)
        snap = MarketSnapshot(
            fixture_id=fixture_id, captured_at=_now(), status="writing",
            policy_version=LOCK_POLICY_VERSION,
            provider_schema_version=PROVIDER_SCHEMA_VERSION,
            events_expected=len(events),
            events_captured=len(events) - len(failed),
            contracts_expected=contracts_expected)
        s.add(snap)
        s.flush()
        quote_by_ticker: dict[str, int] = {}
        depth_rows = with_prices = without_prices = 0
        game_two_sided = 0                   # bid AND ask (execution)
        game_quotes = 0                      # ask present (capture)
        oldest_age = 0                        # over ALL quotes (context)
        game_ages: list[int] = []            # provider ages of PRICED game quotes
        game_priced_no_ts = 0                # priced game quotes w/o a ts
        now = _now()
        for me in events:
            ms = markets_by_event.get(me.kalshi_event_ticker) or []
            raw = json.dumps(ms, sort_keys=True)
            obs = SourceObservation(
                source="kalshi",
                endpoint=f"lock:markets?event={me.kalshi_event_ticker}",
                content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                payload_json=raw[:config.OBSERVATION_PAYLOAD_MAX_BYTES],
                observed_at=now)
            s.add(obs)
            s.flush()
            for m in ms:
                mc = s.query(MarketContract).filter_by(
                    ticker=m.get("ticker")).first()
                if mc is None:
                    continue
                quote = _quote_row(m, mc.id, obs.id, snapshot_id=snap.id)
                quote.source_observation_id = obs.id
                s.add(quote)
                s.flush()
                quote_by_ticker[m.get("ticker")] = quote.id
                if quote.yes_ask_c is not None or quote.yes_bid_c is not None:
                    with_prices += 1
                else:
                    without_prices += 1
                if quote.provider_timestamp is not None:
                    age = int((now - quote.provider_timestamp)
                              .total_seconds())
                    oldest_age = max(oldest_age, age)
                if me.series == SERIES:
                    if quote.yes_ask_c is not None:
                        game_quotes += 1
                        # freshness is measured on the REQUIRED game quotes
                        # specifically (V9 eval F9) — not accidentally
                        # dominated by unrelated optional-family quotes
                        if quote.provider_timestamp is not None:
                            game_ages.append(int(
                                (now - quote.provider_timestamp)
                                .total_seconds()))
                        else:
                            game_priced_no_ts += 1
                    if (quote.yes_ask_c is not None
                            and quote.yes_bid_c is not None):
                        game_two_sided += 1
                for side, price_c, size, price_d, size_fp in _depth_levels(
                        books.get(m.get("ticker")) or {}):
                    s.add(MarketDepthLevel(
                        market_quote_id=quote.id, side=side,
                        price_c=price_c, size=size,
                        price_dollars=price_d, size_fp=size_fp))
                    depth_rows += 1
        # required families for policy v1 = the GAME 3-way (the
        # executable comparator); everything else is captured-when-present
        required_ok = (not failed) and game_quotes >= 3
        # freshness over the REQUIRED game quotes, with an EXPLICIT basis
        # (V9 eval F9): a missing provider timestamp must never read as
        # age zero / "fresh". 'provider' = every priced game quote carried
        # a provider timestamp; 'capture_time' = at least one didn't, so we
        # fall back to our own capture clock (~0s old, but LABELLED as such,
        # never dressed up as a provider-confirmed fresh reading); 'none' =
        # no priced game quote at all.
        # v2: the CAPTURE clock, because the venue publishes no quote-update
        # time (QUOTE_FRESHNESS_NOTE). `now` is the capture instant and every
        # quote in this snapshot was written from the response we just
        # fetched, so the honest age is the age of that fetch. The provider
        # ages are still computed above and retained per quote.
        if game_quotes == 0:
            game_oldest_age = None
            freshness_basis = "none"
        else:
            game_oldest_age = max(
                0, int((_now() - now).total_seconds()))
            freshness_basis = "capture_time"
        snap.quotes_written = len(quote_by_ticker)
        snap.quotes_with_prices = with_prices
        snap.quotes_without_prices = without_prices
        snap.depth_rows_written = depth_rows
        # both age fields are CAPTURE-based in v2 so the risk engine's
        # max_quote_age_s gate measures the same (meaningful) quantity;
        # `oldest_age` above is the provider clock and is deliberately not
        # used as a gate — it is preserved per quote for audit
        snap.oldest_quote_age_seconds = game_oldest_age
        snap.game_oldest_quote_age_seconds = game_oldest_age
        snap.freshness_basis = freshness_basis
        snap.required_families_complete = required_ok
        # execution-ready is DISTINCT from capture-complete: the game
        # comparator must be two-sided (bid AND ask) AND freshly captured.
        #
        # v1 required freshness_basis == 'provider' (V9.1 eval F6). That is
        # unsatisfiable at this venue — Kalshi's updated_time ran ~30h stale
        # on ACTIVE two-sided markets — so the gate could never pass and
        # silently blocked all paper execution (QUOTE_FRESHNESS_NOTE).
        # v2 accepts the capture clock, which is the strongest freshness
        # claim the venue actually supports, and the basis is recorded on
        # the snapshot so no reader mistakes it for a provider-confirmed
        # reading.
        snap.execution_ready = (
            game_two_sided >= 3
            and freshness_basis == "capture_time"
            and game_oldest_age is not None
            and game_oldest_age <= 600)
        # CAPTURE-completeness gate (not tradeability): every event
        # fetched AND the executable 3-way comparator present
        if not required_ok or len(quote_by_ticker) == 0:
            snap.status = "failed"
            snap.failure_reason = (
                f"events_failed={failed} quotes={len(quote_by_ticker)} "
                f"game_quotes_priced={game_quotes}")[:500]
            s.commit()
            print(f"[markets] lock snapshot INCOMPLETE fixture "
                  f"{fixture_id}: {snap.failure_reason}")
            return None
        snap.status = "complete"
        s.commit()
        return {"snapshot_id": snap.id, "quote_by_ticker": quote_by_ticker,
                "execution_ready": snap.execution_ready}
    except Exception as exc:
        s.rollback()
        print(f"[markets] lock snapshot failed: {exc}")
        return None
    finally:
        s.close()
