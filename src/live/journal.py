"""Personal bet journal + execution-fidelity pilot.

Son records the bets he forms a view on. A consenting friend places some
of them on the real Kalshi market at much smaller size. The gap between
the two measures whether this project's EXECUTION model — fees, spread,
fill assumptions — matches reality, because every fill in the paper
ledger is simulated.

WHAT THIS IS NOT
================
Not research evidence, and never edge evidence. The bets are
human-selected: the ones that feel strongest are the ones most likely to
be recorded. That is selection bias by construction. There is no join
from here to PaperSignal/PaperFill, and nothing here reaches model_eval
or an approval decision. The three evidence classes never sum — one is a
simulation, one a counterfactual, one somebody's bank balance.

TWO RULES THAT MAKE THE DATA WORTH ANYTHING
===========================================
1. Record the PASSES. The paper ledger keeps its rejections so nothing is
   selected away; a journal of only the bets that were taken has exactly
   the bias that design prevents. A view is recorded when it FORMS, then
   resolves to taken or passed.

2. A price must be FALSIFIABLE. An entry claiming a price nobody can
   check is a story, not a record — the same error as presenting a
   reconstructed decision as a contemporaneous one. `observed_quote`
   requires a real quote captured at or before the moment of recording;
   anything else is `stated_only` and counted nowhere.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

import config
from src.live.db import get_session, plane_ready
from src.live.models import (BroadcastLog, Fixture, MarketQuote,
                             PersonalBet, PersonalBetExecution)

# Below this many settled executions, report the rows and NO summary
# statistic. A mean over three fills is a story, and this project has
# already learned what happens when a point estimate is published
# without the interval that makes it readable.
MIN_EXECUTIONS_FOR_AGGREGATE = 20

JOURNAL_POLICY = {
    "version": "journal-v1",
    "min_executions_for_aggregate": MIN_EXECUTIONS_FOR_AGGREGATE,
    "evidence_class": "personal_journal",
    "counts_toward_edge": False,
    "note": ("human-selected bets; establishes execution fidelity, never "
             "edge. Never sums with the paper ledger."),
}

STATUSES = ("considered", "taken", "passed", "void")
EXEC_STATUSES = ("filled", "partial", "not_filled")


def _now():
    return datetime.now(timezone.utc)


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _d(v) -> Decimal | None:
    """Lenient decimal parse for INTERNAL reads (values this module
    already stored). Operator input goes through `_dec`, which enforces
    a domain instead of silently swallowing garbage."""
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def _dec(v, field: str, *, lo=None, hi=None,
         lo_exclusive: bool = False):
    """Operator-supplied decimal with a DOMAIN (journal-P0-D).

    Returns (value, error). None/"" is (None, None) — absent, for the
    caller's required-field logic to judge. Anything present must parse,
    be FINITE (NaN/inf are not prices), and sit inside the stated
    domain — a fill at $-1 or $1.01 is not a weaker record, it is a
    data-entry error, refused before anything touches the database."""
    if v in (None, ""):
        return None, None
    try:
        d = Decimal(str(v))
    except (ArithmeticError, TypeError, ValueError):
        return None, f"{field}={v!r} is not a decimal number"
    if not d.is_finite():
        return None, f"{field}={v!r} must be a finite number"
    if lo is not None and (d <= lo if lo_exclusive else d < lo):
        op = ">" if lo_exclusive else ">="
        return None, f"{field}={v!r} out of domain (must be {op} {lo})"
    if hi is not None and d > hi:
        return None, f"{field}={v!r} out of domain (must be <= {hi})"
    return d, None


def _price(v, field):
    """Contract price in dollars: [0, 1]."""
    return _dec(v, field, lo=Decimal(0), hi=Decimal(1))


def _qty(v, field):
    """Contract quantity: strictly positive."""
    return _dec(v, field, lo=Decimal(0), lo_exclusive=True)


def _money(v, field):
    """Fee / settlement credit: non-negative."""
    return _dec(v, field, lo=Decimal(0))


def _aware(v, field: str):
    """Operator-supplied timestamp: must carry an explicit timezone
    (journal-P0-D). Returns (utc_datetime, error). A naive timestamp is
    REJECTED, never silently relabelled UTC — 'assume UTC' converts a
    data-entry mistake into wrong provenance with no trace."""
    if v is None:
        return None, None
    if not isinstance(v, datetime):
        return None, f"{field} must be a datetime"
    if v.tzinfo is None:
        return None, (f"{field} is timezone-naive — supply an explicit "
                      f"offset; the server will not guess UTC")
    return v.astimezone(timezone.utc), None


def _content_hash(doc: dict) -> str:
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


# --- canonical payload hashing (journal-P0-I) ------------------------------
# Idempotency was keyed on (exchange_order_id, status) alone, so a retry
# carrying DIFFERENT economics got the earlier row back as though that
# row had confirmed the new payload. On a third party's money that is
# the worst possible failure: two contradictory accounts of one fill,
# and the API reported agreement.
#
# So every idempotent write path hashes the payload it accepted. An
# exact repeat is a no-op; anything else is a CONFLICT that writes
# nothing. The hash is over VALUES, not their spelling: "0.47" and
# "0.470" are the same fill, and a retry must not fail on formatting.

def _canon(v):
    """One payload value in canonical form.

    Decimals compare by VALUE (0.47 == 0.470 == 0.4700), datetimes in
    UTC, everything else as its string. This is what makes the hash a
    statement about the economics rather than about the JSON."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        d = v.normalize()
        exp = d.as_tuple().exponent
        if isinstance(exp, int) and exp > 0:
            # normalize() renders 100 as 1E+2; re-expand so the exact
            # same quantity never hashes two ways
            d = d.quantize(Decimal(1))
        return format(d, "f")
    if isinstance(v, datetime):
        return _utc(v).isoformat()
    return str(v)


def _payload_hash(kind: str, fields: dict) -> str:
    doc = {"__kind__": kind}
    doc.update({k: _canon(v) for k, v in fields.items()})
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _same_payload(stored: str | None, incoming: str) -> bool:
    import hmac
    return bool(stored) and hmac.compare_digest(str(stored), incoming)


def _conflict(what: str, stored_hash: str, incoming_hash: str,
              detail: str) -> dict:
    """A refused retry. `conflict` is what the route turns into HTTP 409;
    the two hashes let an operator see that the server compared payloads
    rather than guessing."""
    return {"conflict": True, "error": detail,
            "conflicting_write": what,
            "recorded_payload_hash": stored_hash,
            "submitted_payload_hash": incoming_hash}


# --- quote capture age (journal-P0-H) --------------------------------------
# Kalshi publishes no quote timestamp (the `updated_time` field is the
# market DEFINITION clock and can be ~30h stale on an ACTIVE market), so
# our capture clock is the ONLY evidence of when a book was observed.
# Without a ceiling, a same-contract quote of any age could be promoted
# into `observed_quote` and into the decomposed execution-fidelity
# metrics — an ancient capture silently becoming "the book at the time".

def _age_seconds(captured_at, reference) -> int | None:
    if captured_at is None or reference is None:
        return None
    return int((_utc(reference) - _utc(captured_at)).total_seconds())


def _quote_freshness(row) -> str:
    """Whether a recorded entry's cited book is still admissible:
    none | fresh | stale | unknown.

    Applied at READ time as well as write time, deliberately. Fixing the
    writer alone would leave every row recorded before the ceiling
    existed still counting toward aggregates off an arbitrarily old
    book — the reader is where those rows are actually used.

    The ceiling STORED on the row wins over the current config: a row
    accepted under one ceiling must not be reclassified when the config
    moves, which would rewrite history."""
    if row.price_basis != "observed_quote":
        return "none"
    ceiling = row.quote_max_age_seconds
    if ceiling is None:
        ceiling = int(config.JOURNAL_QUOTE_MAX_AGE_SECONDS)
    age = row.quote_age_seconds
    if age is None:      # pre-ceiling row: derive from what it carries
        age = _age_seconds(row.quote_observed_at, row.recorded_at)
    if age is None:
        return "unknown"
    return "fresh" if age <= ceiling else "stale"


def _fill_quote_freshness(s, ex) -> str:
    """The same question for an execution's fill book, measured against
    the FILL moment rather than now."""
    if ex.market_quote_id_at_fill is None:
        return "none"
    ceiling = ex.fill_quote_max_age_seconds
    if ceiling is None:
        ceiling = int(config.JOURNAL_FILL_QUOTE_MAX_AGE_SECONDS)
    age = ex.fill_quote_age_seconds
    if age is None:
        q = s.get(MarketQuote, ex.market_quote_id_at_fill)
        age = _age_seconds(q.captured_at if q else None, ex.filled_at)
    if age is None:
        return "unknown"
    return "fresh" if age <= ceiling else "stale"


def _counts_toward_aggregate(row, superseded: bool) -> bool:
    """The ONE predicate every surface uses. An entry counts only when
    its price is falsifiable (observed_quote), the view was a forecast
    (not void), it is the head of its correction chain, and the book it
    cites was CONTEMPORANEOUS with the view (journal-P0-H). `unknown`
    freshness never counts — missing evidence is not confidence."""
    return (row.price_basis == "observed_quote"
            and row.status != "void"
            and not superseded
            and _quote_freshness(row) == "fresh")


def _validate_quote_identity(s, fixture_id: int, market_ticker: str,
                             outcome_key: str | None,
                             market_contract_id: int | None,
                             q) -> tuple[dict | None, int | None]:
    """journal-P0 F3: an EXISTING quote cited for this view must belong
    to this fixture's approved market chain — fixture → approved
    MarketEvent → MarketContract → quote — and to the stated contract
    and outcome. A quote that belongs elsewhere is REJECTED with the
    mismatch named, never silently accepted: the whole point of citing
    a quote is falsifiability, and a price from another match falsifies
    nothing.

    Returns (error_dict, resolved_contract_id). error_dict is None when
    the quote belongs.
    """
    from src.live.models import MarketContract, MarketEvent
    mc = (s.get(MarketContract, q.market_contract_id)
          if q.market_contract_id else None)
    me = s.get(MarketEvent, mc.market_event_id) if mc else None
    if mc is None or me is None:
        return ({"error": f"quote {q.id} has no resolvable market "
                          f"contract/event chain — cannot verify it "
                          f"belongs to fixture {fixture_id}"}, None)
    if me.fixture_id != fixture_id:
        return ({"error": f"quote {q.id} belongs to fixture "
                          f"{me.fixture_id} (event "
                          f"{me.kalshi_event_ticker}), not fixture "
                          f"{fixture_id} — refusing to record a price "
                          f"from another match"}, None)
    if not me.mapping_approved:
        return ({"error": f"quote {q.id} sits on event "
                          f"{me.kalshi_event_ticker} whose fixture "
                          f"mapping is not approved — refusing an "
                          f"unverified market identity"}, None)
    if market_contract_id is not None and market_contract_id != mc.id:
        return ({"error": f"quote {q.id} belongs to market contract "
                          f"{mc.id} ({mc.ticker}), not the stated "
                          f"contract {market_contract_id}"}, None)
    if mc.ticker and market_ticker and mc.ticker != market_ticker:
        return ({"error": f"quote {q.id} prices contract {mc.ticker}, "
                          f"not the stated ticker {market_ticker}"},
                None)
    if outcome_key and mc.outcome_key and mc.outcome_key != outcome_key:
        return ({"error": f"quote {q.id} prices outcome "
                          f"{mc.outcome_key}, not the stated outcome "
                          f"{outcome_key}"}, None)
    return None, mc.id


def _resolve_or_create_viewer_fixture(s, competition_slug: str,
                                      provider_fixture_id: str, *,
                                      kickoff_utc=None,
                                      home_team: str | None = None,
                                      away_team: str | None = None):
    """A Fixture row for a VIEWER competition, addressed by provider id.

    The journal could only ever record MLS, because a pick had to name a
    `fixture.id` and only the MLS ingest creates those. Leagues Cup ran
    live with no way to record anything (journal-P0-M). The viewer
    surfaces address a match by `(competition, provider fixture id)`, so
    that is what this accepts.

    Get-or-CREATE, minimally: enough row to hang an immutable pick on and
    nothing more. It does NOT pretend to be ingest — no model run, no
    market chain, no approved MarketEvent — which is exactly why an entry
    made this way can only ever be `stated_only` and is excluded from
    every aggregate BY NAME. Creating the row asserts that a pick was
    made about this match, never that the platform models it.

    Returns (fixture, error_dict). Teams are stored as NAMED, via the
    same unique `(competition_slug, canonical_name)` Team rows the rest
    of the plane uses, so a viewer pick does not invent a second team
    namespace.
    """
    from src.live.models import Competition, Team
    slug = (competition_slug or "").strip()
    pid = str(provider_fixture_id or "").strip()
    if not slug or not pid:
        return None, {"error": "competition_slug and provider_fixture_id "
                               "are both required to address a viewer "
                               "fixture"}
    comp = s.get(Competition, slug)
    if comp is None:
        # season is NOT NULL and is not knowable from a fixture id, so it
        # is derived from the slug's trailing year when present and left
        # explicit rather than invented when it is not.
        year = None
        tail = slug.rsplit("-", 1)[-1]
        if tail.isdigit() and len(tail) == 4:
            year = int(tail)
        if year is None:
            return None, {"error": f"competition {slug!r} is not seeded and "
                                   f"its season cannot be read from the "
                                   f"slug — seed the competition first "
                                   f"rather than guessing a season"}
        comp = Competition(slug=slug, name=slug, season=year)
        s.add(comp)
        s.flush()

    fx = (s.query(Fixture)
          .filter_by(competition_slug=slug, provider_fixture_id=pid)
          .first())
    if fx is not None:
        return fx, None

    def _team(name):
        if not name:
            return None
        row = (s.query(Team)
               .filter_by(competition_slug=slug,
                          canonical_name=name[:80]).first())
        if row is None:
            row = Team(competition_slug=slug, canonical_name=name[:80])
            s.add(row)
            s.flush()
        return row.id

    fx = Fixture(competition_slug=slug, provider_fixture_id=pid,
                 status="pre", current_kickoff_utc=_aware_or_none(kickoff_utc),
                 home_team_id=_team(home_team), away_team_id=_team(away_team))
    s.add(fx)
    s.flush()
    return fx, None


def _aware_or_none(v):
    """A tz-aware datetime, or None. A naive kickoff would make the
    after-kickoff `void` check compare across tz-awareness and raise."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if getattr(v, "tzinfo", None) is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def record_view(fixture_id: int | None, market_ticker: str, *,
                outcome_key: str | None = None,
                stated_price=None, stated_size=None,
                rationale: str | None = None,
                market_quote_id: int | None = None,
                market_contract_id: int | None = None,
                corrects_bet_id: int | None = None,
                competition_slug: str | None = None,
                provider_fixture_id: str | None = None,
                kickoff_utc=None,
                home_team: str | None = None,
                away_team: str | None = None) -> dict:
    """Record a view at the moment it FORMS.

    Returns the stored row as a dict, including the price_basis actually
    granted — which may be weaker than the caller hoped. The rules below
    are enforced here rather than trusted to callers:

    - `observed_quote` is granted only when a real quote exists, was
      captured at or before now, AND belongs to this fixture's approved
      market chain (fixture → approved event → contract → quote). A
      quote that does not exist or postdates now downgrades the entry
      to `stated_only` (falsifiability rule); a quote that exists but
      belongs to ANOTHER fixture/contract/outcome is rejected outright
      with the mismatch named (journal-P0 F3) — that is an identity
      error, not a weaker record.
    - recording after kickoff yields `void`: keepable, never counted.
    - the model's probability is FROZEN now, with the run it came from,
      so a later re-run cannot change what was recorded.
    - `corrects_bet_id` names the immutable entry this row corrects
      (journal-P0 F5); corrections are new rows, never rewrites. A
      correction restates the SAME observation, so it must share the
      corrected entry's competition, fixture, market and outcome, and
      chains are linear — an already-corrected entry cannot be
      corrected again (journal-P1-F). Aggregates then count one
      effective observation per chain; the corrected rows remain as
      audit.
    - every new view starts `considered` (journal-P0-D): there is no
      way to record a pre-resolved entry, because considered→resolve is
      what makes the passes recordable at all.
    """
    if not plane_ready():
        return {"error": "dormant"}
    # journal-P0-D: operator-supplied values are validated BEFORE any
    # row is built — a rejection writes nothing
    price, err = _price(stated_price, "stated_price")
    if err:
        return {"error": err}
    size, err = _qty(stated_size, "stated_size")
    if err:
        return {"error": err}
    status = "considered"
    s = get_session()
    try:
        if fixture_id is not None:
            # the MLS path, unchanged: an id addresses an ingested row
            fx = s.get(Fixture, fixture_id)
            if fx is None:
                return {"error": "no such fixture"}
        else:
            # viewer competitions address a match by provider id
            # (journal-P0-M); nothing here can reach the branch above
            fx, err = _resolve_or_create_viewer_fixture(
                s, competition_slug, provider_fixture_id,
                kickoff_utc=kickoff_utc, home_team=home_team,
                away_team=away_team)
            if err:
                return err
            fixture_id = fx.id
        if corrects_bet_id is not None:
            prev = s.get(PersonalBet, corrects_bet_id)
            if prev is None:
                return {"error": f"corrects_bet_id {corrects_bet_id} "
                                 f"names no existing entry"}
            # journal-P1-F: a correction RESTATES the same observation,
            # so it must share the corrected entry's scope exactly —
            # competition, fixture, market and outcome. A "correction"
            # pointing across any of those is a different observation
            # wearing supersession semantics, and in a competition-
            # scoped corpus it would export as a dangling reference.
            for what, prev_v, new_v in (
                    ("competition", prev.competition_slug,
                     fx.competition_slug),
                    ("fixture", prev.fixture_id, fixture_id),
                    ("market_ticker", prev.market_ticker,
                     market_ticker),
                    ("outcome_key", prev.outcome_key or None,
                     outcome_key or None)):
                if prev_v != new_v:
                    return {"error":
                            f"corrects_bet_id {corrects_bet_id} is out "
                            f"of scope: entry {corrects_bet_id} has "
                            f"{what}={prev_v!r}, this view names "
                            f"{new_v!r} — a correction restates the "
                            f"SAME observation (same competition, "
                            f"fixture, market and outcome); a "
                            f"different view is a new entry, not a "
                            f"correction"}
            # chains stay LINEAR (journal-P1-F): one effective
            # observation per chain requires exactly one head, so an
            # already-corrected entry cannot be corrected twice
            head = (s.query(PersonalBet)
                    .filter_by(corrects_bet_id=corrects_bet_id)
                    .first())
            if head is not None:
                return {"error":
                        f"entry {corrects_bet_id} is already corrected "
                        f"by entry {head.id} — correction chains are "
                        f"linear; correct the head "
                        f"(corrects_bet_id={head.id})"}
        now = _now()

        # 1. price basis — falsifiable or explicitly not
        basis, quote_at = "stated_only", None
        cited_quote_id = market_quote_id
        quote_age, quote_reject = None, None
        ceiling = int(config.JOURNAL_QUOTE_MAX_AGE_SECONDS)
        if market_quote_id is not None:
            q = s.get(MarketQuote, market_quote_id)
            if q is None:
                market_quote_id, quote_reject = None, "not_found"
            elif q.captured_at is None:
                market_quote_id, quote_reject = None, "no_capture_time"
            elif _utc(q.captured_at) > now:
                market_quote_id, quote_reject = None, "future_capture"
            else:
                # identity BEFORE age: a quote from another match is an
                # identity error at any age, and calling it "stale"
                # would name the wrong fault
                err, resolved_mc = _validate_quote_identity(
                    s, fixture_id, market_ticker, outcome_key,
                    market_contract_id, q)
                if err is not None:
                    return err
                age = _age_seconds(q.captured_at, now)
                if age > ceiling:
                    # journal-P0-H: refused as the price basis AND said.
                    # The entry is still recorded — rule 1 is that views
                    # get recorded — but it counts nowhere and the
                    # reason is on the row, never an absent field.
                    market_quote_id = None
                    quote_reject, quote_age = "stale_capture", age
                else:
                    market_contract_id = resolved_mc
                    basis, quote_at = "observed_quote", _utc(q.captured_at)
                    quote_age = age

        # 2. after kickoff a view is not a forecast
        ko = _utc(fx.current_kickoff_utc)
        if ko is not None and now >= ko:
            status = "void"

        # 3. freeze the model's read NOW
        model_p, run_id = _frozen_model_probability(
            s, fixture_id, outcome_key)

        row = PersonalBet(
            competition_slug=fx.competition_slug, fixture_id=fixture_id,
            market_ticker=market_ticker,
            market_contract_id=market_contract_id,
            outcome_key=outcome_key, status=status,
            stated_price_dollars=(str(price)
                                  if price is not None else None),
            stated_size=(str(size) if size is not None else None),
            price_basis=basis, market_quote_id=market_quote_id,
            quote_observed_at=quote_at, recorded_at=now,
            rationale=rationale, model_probability=model_p,
            prediction_run_id=run_id, corrects_bet_id=corrects_bet_id,
            # journal-P0-H: the age actually measured and the ceiling
            # that was applied travel WITH the row
            quote_age_seconds=quote_age,
            quote_max_age_seconds=(ceiling if cited_quote_id is not None
                                   else None),
            quote_rejection_reason=quote_reject)
        row.content_hash = _content_hash({
            "fixture_id": fixture_id, "market_ticker": market_ticker,
            "outcome_key": outcome_key,
            "stated_price": row.stated_price_dollars,
            "stated_size": row.stated_size, "price_basis": basis,
            "market_quote_id": market_quote_id,
            "quote_age_seconds": quote_age,
            "quote_rejection_reason": quote_reject,
            "recorded_at": now.isoformat(),
            "model_probability": model_p, "prediction_run_id": run_id})
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            # journal-P1-G: the read-then-insert head check above loses
            # the race — two concurrent corrections of one entry both
            # read "no head". The partial unique index settles it in the
            # database; translate its refusal into the same message the
            # sequential path gives, so concurrency is not a different
            # API contract.
            s.rollback()
            if corrects_bet_id is not None:
                head = (s.query(PersonalBet)
                        .filter_by(corrects_bet_id=corrects_bet_id)
                        .first())
                if head is not None:
                    return {"error":
                            f"entry {corrects_bet_id} is already corrected "
                            f"by entry {head.id} — correction chains are "
                            f"linear; correct the head "
                            f"(corrects_bet_id={head.id})"}
            raise
        return _bet_dict(row)
    finally:
        s.close()


def resolve_view(bet_id: int, status: str) -> dict:
    """Move a recorded view from `considered` to `taken` or `passed`.
    The pass is the half that keeps the journal honest, so it is a
    first-class transition.

    A resolution is IMMUTABLE once set (journal-P0 F5): taken→passed→
    taken rewrites would let the journal remember its winners, which is
    the exact bias the record exists to prevent. A mistaken resolution
    is corrected by a NEW view citing `corrects_bet_id`."""
    if status not in ("taken", "passed"):
        return {"error": "resolve to taken or passed"}
    if not plane_ready():
        return {"error": "dormant"}
    s = get_session()
    try:
        row = s.get(PersonalBet, bet_id)
        if row is None:
            return {"error": "no such entry"}
        if row.status == "void":
            return {"error": "entry is void (recorded after kickoff)"}
        if row.status != "considered":
            return {"error": f"entry {bet_id} is already resolved to "
                             f"'{row.status}' — resolutions are "
                             f"immutable. Record a correction as a NEW "
                             f"view with corrects_bet_id={bet_id}"}
        row.status = status
        row.resolved_at = _now()
        s.commit()
        sup = (s.query(PersonalBet)
               .filter_by(corrects_bet_id=row.id).first() is not None)
        return _bet_dict(row, superseded=sup)
    finally:
        s.close()


def _frozen_model_probability(s, fixture_id: int,
                              outcome_key: str | None):
    """The shadow model's probability for this outcome right now, with
    the run it came from. Frozen onto the entry so a later re-run cannot
    retroactively change what was recorded."""
    if not outcome_key:
        return None, None
    from src.live.models import PredictionContract, PredictionRun
    run = (s.query(PredictionRun)
           .filter_by(fixture_id=fixture_id, status="complete")
           .order_by(PredictionRun.captured_at.desc()).first())
    if run is None:
        return None, None
    c = (s.query(PredictionContract)
         .filter_by(prediction_run_id=run.id, outcome_key=outcome_key)
         .first())
    return (c.raw_probability if c else None), run.id


def record_execution(bet_id: int, account_label: str, *,
                     consent_recorded_at,
                     status: str = "filled",
                     fill_price=None, filled_contracts=None,
                     fee_paid=None, filled_at=None,
                     market_quote_id_at_fill: int | None = None,
                     not_filled_reason: str | None = None,
                     best_available_price=None,
                     exchange_order_id: str | None = None,
                     publication_consent: bool = False) -> dict:
    """Record a REAL fill — or a real failure to fill.

    The lifecycle is append-only (journal-P0 F5):

        considered → taken → filled|partial → settled → reconciled

    so an execution attaches ONLY to a `taken` view — a fill against a
    passed or void entry describes a bet nobody decided to place, and
    accepting it would let the journal invent history.

    `consent_recorded_at` is required and never defaulted: this is
    someone else's money, and the provenance of that consent belongs in
    the row rather than in someone's memory — supplied by the operator,
    never manufactured by the server.

    A `filled`/`partial` row must carry its economics — price, quantity,
    fee — and the ACTUAL fill timestamp: an execution-fidelity pilot
    with fabricated fill facts measures nothing.

    A `not_filled` row is as valuable as a fill. It is evidence about
    liquidity, which is half of what this pilot exists to measure.

    Idempotency: (exchange_order_id, status) is unique. A retried POST
    for a pair already recorded returns the existing row as a no-op.

    Validation ordering (journal-P0-D): every value-domain, timezone,
    lifecycle, chronology and quote-identity check runs BEFORE any row
    is built — a rejection writes exactly nothing. The fill book cited
    by `market_quote_id_at_fill` must belong to the PARENT BET's
    fixture/contract/outcome and be captured at or before the fill
    moment (journal-P0-E) — the same identity discipline the view-time
    quote gets.
    """
    if not plane_ready():
        return {"error": "dormant"}
    # --- parameter validation: nothing touches the DB past a bad value
    if status not in EXEC_STATUSES:
        return {"error": f"status must be one of {EXEC_STATUSES}"}
    if not consent_recorded_at:
        return {"error": "consent_recorded_at is required — this is a "
                         "third party's money"}
    consent_at, err = _aware(consent_recorded_at, "consent_recorded_at")
    if err:
        return {"error": err}
    filled_at_utc, err = _aware(filled_at, "filled_at")
    if err:
        return {"error": err}
    price, err = _price(fill_price, "fill_price")
    if err:
        return {"error": err}
    qty, err = _qty(filled_contracts, "filled_contracts")
    if err:
        return {"error": err}
    fee, err = _money(fee_paid, "fee_paid")
    if err:
        return {"error": err}
    best, err = _price(best_available_price, "best_available_price")
    if err:
        return {"error": err}
    if not account_label:
        return {"error": "account_label is required"}
    if status == "not_filled" and not not_filled_reason:
        return {"error": "not_filled requires a reason"}
    if status in ("filled", "partial"):
        missing = [name for name, v in (
            ("fill_price", price), ("filled_contracts", qty),
            ("fee_paid", fee), ("filled_at", filled_at_utc))
            if v is None]
        if missing:
            return {"error": f"a '{status}' execution requires its real "
                             f"economics and fill moment — missing: "
                             f"{', '.join(missing)}. The server never "
                             f"invents a fill fact"}
    # journal-P1-H: combinations that describe an impossible event are
    # refused, not stored. A row asserting both "nothing filled" and a
    # fill price is not a weaker record — it is two contradictory claims
    # about a third party's money, and gaps_for would happily compute a
    # latency for a fill that never happened.
    if status == "not_filled":
        contradictory = [n for n, v in (
            ("fill_price", price), ("filled_contracts", qty),
            ("fee_paid", fee), ("filled_at", filled_at_utc),
            ("market_quote_id_at_fill", market_quote_id_at_fill))
            if v is not None]
        if contradictory:
            return {"error": f"a 'not_filled' execution cannot carry fill "
                             f"facts — remove {', '.join(contradictory)}. "
                             f"Nothing filled, so there is no fill price, "
                             f"quantity, fee, moment or book. The "
                             f"liquidity evidence is the reason and "
                             f"best_available_price"}
    elif not_filled_reason:
        return {"error": f"a '{status}' execution cannot carry "
                         f"not_filled_reason={not_filled_reason!r} — it "
                         f"filled"}
    # journal-P1-H: time runs forward at BOTH ends. Round 2 stopped a
    # fill antedating its decision, but nothing stopped a fill, a
    # consent record or a settlement from postdating its own recording —
    # and a filled_at in the future silently produced a NEGATIVE
    # latency_seconds in the fidelity metrics.
    #
    # Deliberately NOT checked here: whether consent precedes the fill.
    # The column records when consent was RECORDED, not when it was
    # given, and an operator writing the provenance down after the fill
    # is ordinary practice, not a contradiction. Asserting an ordering
    # the field does not claim would refuse honest rows.
    horizon = _now() + timedelta(
        seconds=config.JOURNAL_FUTURE_TOLERANCE_SECONDS)
    for name, when in (("consent_recorded_at", consent_at),
                       ("filled_at", filled_at_utc)):
        if when is not None and when > horizon:
            return {"error": f"{name} {when.isoformat()} is in the "
                             f"future beyond clock-skew tolerance — a "
                             f"recorded fact cannot postdate its own "
                             f"recording"}
    s = get_session()
    try:
        bet = s.get(PersonalBet, bet_id)
        if bet is None:
            return {"error": "no such entry"}
        if bet.status != "taken":
            return {"error": f"entry {bet_id} is '{bet.status}' — an "
                             f"execution attaches only to a taken view "
                             f"(considered → taken → filled|partial → "
                             f"settled → reconciled)"}
        # chronology (journal-P0-D): the fill cannot precede the
        # decision it executes
        if filled_at_utc is not None and bet.resolved_at is not None \
                and filled_at_utc < _utc(bet.resolved_at):
            return {"error": f"filled_at {filled_at_utc.isoformat()} "
                             f"precedes the taken moment "
                             f"{_utc(bet.resolved_at).isoformat()} — a "
                             f"fill cannot antedate the decision it "
                             f"executes"}
        # fill-book identity (journal-P0-E): the SAME chain check the
        # view-time quote gets, against the parent bet
        fill_age, fill_ceiling = None, None
        if market_quote_id_at_fill is not None:
            fq = s.get(MarketQuote, market_quote_id_at_fill)
            if fq is None:
                return {"error": f"market_quote_id_at_fill "
                                 f"{market_quote_id_at_fill} names no "
                                 f"stored quote — the fill book must "
                                 f"be a persisted observation"}
            id_err, _mc = _validate_quote_identity(
                s, bet.fixture_id, bet.market_ticker, bet.outcome_key,
                bet.market_contract_id, fq)
            if id_err is not None:
                return id_err
            cap = _utc(fq.captured_at)
            if cap is None:
                return {"error": f"fill quote {fq.id} has no capture "
                                 f"time — unusable as the book at fill"}
            ref = filled_at_utc if filled_at_utc is not None else _now()
            if cap > ref:
                return {"error": f"fill quote {fq.id} was captured "
                                 f"{cap.isoformat()}, AFTER the fill "
                                 f"moment {ref.isoformat()} — a book "
                                 f"observed after the fill cannot be "
                                 f"the book at fill"}
            # journal-P0-H: and it must be CONTEMPORANEOUS with the fill.
            # Round 2 bounded this on one side only, so a quote from any
            # distance in the past passed and became "the book at fill"
            # for market_drift and execution_cost.
            fill_ceiling = int(config.JOURNAL_FILL_QUOTE_MAX_AGE_SECONDS)
            fill_age = _age_seconds(cap, ref)
            if fill_age > fill_ceiling:
                return {"error": f"fill quote {fq.id} was captured "
                                 f"{fill_age}s before the fill moment "
                                 f"{ref.isoformat()}, beyond the "
                                 f"{fill_ceiling}s contemporaneity "
                                 f"ceiling — Kalshi publishes no quote "
                                 f"timestamp, so an old capture cannot "
                                 f"stand in for the book at fill. Cite a "
                                 f"contemporaneous quote, or record the "
                                 f"fill without one and read the "
                                 f"no_fill_book classification"}
        # journal-P0-I: the idempotency key is (exchange_order_id,
        # status) + the CANONICAL PAYLOAD. Same key, same economics is a
        # retry; same key, different economics is contradictory evidence
        # and is refused rather than answered with the earlier row.
        payload_hash = _payload_hash("execution", {
            "personal_bet_id": bet_id, "account_label": account_label,
            "consent_recorded_at": consent_at, "status": status,
            "fill_price": price, "filled_contracts": qty,
            "fee_paid": fee, "filled_at": filled_at_utc,
            "market_quote_id_at_fill": market_quote_id_at_fill,
            "not_filled_reason": not_filled_reason,
            "best_available_price": best,
            "exchange_order_id": exchange_order_id,
            "publication_consent": bool(publication_consent)})
        if exchange_order_id:
            existing = (s.query(PersonalBetExecution)
                        .filter_by(exchange_order_id=exchange_order_id,
                                   status=status).first())
            if existing is not None:
                # a row written before this check carries no hash;
                # recompute it from the STORED columns rather than
                # assuming agreement
                stored = (existing.execution_payload_hash
                          or _execution_payload_hash_from_row(existing))
                if not _same_payload(stored, payload_hash):
                    return _conflict(
                        "execution", stored, payload_hash,
                        f"exchange order {exchange_order_id} with status "
                        f"'{status}' is already recorded with DIFFERENT "
                        f"values (execution {existing.id}). A retry must "
                        f"repeat the payload exactly; differing "
                        f"economics are two accounts of one fill, not a "
                        f"retry. Nothing was written — reconcile the "
                        f"discrepancy against the exchange record")
                out = _execution_dict(s, existing)
                out["idempotent"] = True
                out["note"] = (f"exchange order {exchange_order_id} with "
                               f"status '{status}' is already recorded "
                               f"with an identical payload — retry "
                               f"treated as a no-op")
                return out
        row = PersonalBetExecution(
            personal_bet_id=bet_id, account_label=account_label,
            consent_recorded_at=consent_at,
            status=status, not_filled_reason=not_filled_reason,
            best_available_price_dollars=(str(best)
                                          if best is not None else None),
            fill_price_dollars=(str(price)
                                if price is not None else None),
            filled_contracts=(str(qty) if qty is not None else None),
            fee_paid_dollars=(str(fee) if fee is not None else None),
            filled_at=filled_at_utc,
            market_quote_id_at_fill=market_quote_id_at_fill,
            exchange_order_id=exchange_order_id,
            publication_consent=bool(publication_consent),
            fill_quote_age_seconds=fill_age,
            fill_quote_max_age_seconds=fill_ceiling,
            execution_payload_hash=payload_hash)
        s.add(row)
        s.commit()
        return _execution_dict(s, row)
    finally:
        s.close()


def _execution_payload_hash_from_row(row) -> str:
    """The canonical execution hash recomputed from a STORED row.

    Rows written before journal-P0-I carry no hash, and a retry against
    one must still be checked — treating "no recorded hash" as "matches"
    would reintroduce the defect for exactly the rows most likely to be
    retried. Recomputing also means the comparison is against what the
    database actually holds, not against a remembered summary of it."""
    return _payload_hash("execution", {
        "personal_bet_id": row.personal_bet_id,
        "account_label": row.account_label,
        "consent_recorded_at": _utc(row.consent_recorded_at),
        "status": row.status,
        "fill_price": _d(row.fill_price_dollars),
        "filled_contracts": _d(row.filled_contracts),
        "fee_paid": _d(row.fee_paid_dollars),
        "filled_at": _utc(row.filled_at),
        "market_quote_id_at_fill": row.market_quote_id_at_fill,
        "not_filled_reason": row.not_filled_reason,
        "best_available_price": _d(row.best_available_price_dollars),
        "exchange_order_id": row.exchange_order_id,
        "publication_consent": bool(row.publication_consent)})


def settle_execution(execution_id: int, *, settlement_credit,
                     settled_at, settled_outcome: str | None = None,
                     ) -> dict:
    """Record the exchange's settlement of one REAL execution — the
    writer the settlement columns shipped without (journal-P0 F5).

    Transitions filled|partial → settled, append-only: settlement facts
    are written once and never rewritten. A retried call with identical
    values is a no-op; a call with different values is refused — a
    settlement correction is a reconciliation note, not an overwrite.

    `settled_at` and `settlement_credit` come from the exchange record,
    supplied by the operator; the server invents neither. An optional
    `settled_outcome` records the market's resolved outcome on the
    parent entry (write-once as well) so settlement_delta becomes
    computable."""
    if not plane_ready():
        return {"error": "dormant"}
    credit, err = _money(settlement_credit, "settlement_credit")
    if err:
        return {"error": err}
    when, err = _aware(settled_at, "settled_at")
    if err:
        return {"error": err}
    if credit is None or when is None:
        return {"error": "settlement requires settlement_credit and the "
                         "exchange's settled_at — the server never "
                         "invents a settlement fact"}
    if when > _now() + timedelta(
            seconds=config.JOURNAL_FUTURE_TOLERANCE_SECONDS):
        return {"error": f"settled_at {when.isoformat()} is in the "
                         f"future beyond clock-skew tolerance — a market "
                         f"that has not settled yet has no settlement "
                         f"record to copy"}
    payload_hash = _payload_hash("settlement", {
        "settlement_credit": credit, "settled_at": when,
        "settled_outcome": settled_outcome or None})
    s = get_session()
    try:
        row = s.get(PersonalBetExecution, execution_id)
        if row is None:
            return {"error": "no such execution"}
        if row.status not in ("filled", "partial"):
            return {"error": f"execution {execution_id} is "
                             f"'{row.status}' — only filled or partial "
                             f"executions settle"}
        # chronology (journal-P0-D): settlement cannot precede the fill
        if row.filled_at is not None and when < _utc(row.filled_at):
            return {"error": f"settled_at {when.isoformat()} precedes "
                             f"the fill moment "
                             f"{_utc(row.filled_at).isoformat()} — a "
                             f"market cannot settle before the fill"}
        bet = s.get(PersonalBet, row.personal_bet_id)
        # journal-P1-H: a binary contract pays $1 or $0, so the credit
        # can never exceed the contracts filled. A credit above that is
        # a data-entry error, and settlement_delta would silently carry
        # it into the fidelity metrics as a measured discrepancy.
        held = _d(row.filled_contracts)
        if held is not None and credit > held:
            return {"error": f"settlement_credit {credit} exceeds the "
                             f"maximum payout for {held} binary "
                             f"contracts (${held}) — a contract settles "
                             f"at $1 or $0"}
        if row.settled_at is not None:
            # journal-P0-I: payload-sensitive. The old check compared
            # credit and timestamp only, so a retry carrying a DIFFERENT
            # settled_outcome was answered "idempotent" — the outcome
            # conflict check below was never reached.
            stored = (row.settlement_payload_hash
                      or _payload_hash("settlement", {
                          "settlement_credit": _d(
                              row.settlement_credit_dollars),
                          "settled_at": _utc(row.settled_at),
                          "settled_outcome": (bet.settled_outcome or None)
                          if bet is not None else None}))
            if not _same_payload(stored, payload_hash):
                return _conflict(
                    "settlement", stored, payload_hash,
                    f"execution {execution_id} is already settled "
                    f"({row.settlement_credit_dollars} at "
                    f"{_utc(row.settled_at).isoformat()}) and this call "
                    f"differs — settlement is immutable, so a differing "
                    f"payload is a conflict, not a retry. Nothing was "
                    f"written; record the discrepancy in reconciliation")
            out = _execution_dict(s, row)
            out["idempotent"] = True
            return out
        if settled_outcome:
            if bet.settled_outcome and bet.settled_outcome \
                    != settled_outcome:
                return {"error": f"entry {bet.id} already settled as "
                                 f"'{bet.settled_outcome}' — refusing "
                                 f"the conflicting outcome "
                                 f"'{settled_outcome}'"}
            bet.settled_outcome = settled_outcome
            if bet.settled_at is None:
                bet.settled_at = when
        row.settlement_credit_dollars = str(credit)
        row.settled_at = when
        row.settlement_payload_hash = payload_hash
        s.commit()
        return _execution_dict(s, row)
    finally:
        s.close()


def reconcile_execution(execution_id: int, *, note: str,
                        publication_consent: bool | None = None) -> dict:
    """Mark one settled execution as reconciled against the exchange's
    own record — the last state of the append-only lifecycle
    (journal-P0 F5), and the second writer the reconciliation columns
    lacked.

    Requires settlement first: reconciling an unsettled fill would
    assert agreement with an exchange record that does not exist yet.
    An IDENTICAL repeat is a no-op; a repeat carrying a different note
    or a different `publication_consent` is a CONFLICT (journal-P0-I).
    The old check looked only at the `reconciled` flag, so a second call
    changing either field returned success and wrote nothing — the
    publication consent of a third party's financial record could be
    "granted" by a call the server silently discarded.

    `publication_consent` may be set here explicitly (it defaults false
    at recording); it is the only field this transition may change
    besides the reconciliation itself."""
    if not plane_ready():
        return {"error": "dormant"}
    if not note:
        return {"error": "reconciliation requires a note naming what "
                         "was checked against the exchange record"}
    payload_hash = _payload_hash("reconciliation", {
        "note": note, "publication_consent": publication_consent})
    s = get_session()
    try:
        row = s.get(PersonalBetExecution, execution_id)
        if row is None:
            return {"error": "no such execution"}
        if row.status not in ("filled", "partial"):
            return {"error": f"execution {execution_id} is "
                             f"'{row.status}' — only filled or partial "
                             f"executions reconcile"}
        if row.settled_at is None:
            return {"error": f"execution {execution_id} is not settled "
                             f"— reconcile follows settlement "
                             f"(filled|partial → settled → reconciled)"}
        if row.reconciled:
            stored = (row.reconciliation_payload_hash
                      or _payload_hash("reconciliation",
                                       {"note": row.reconciliation_note,
                                        "publication_consent": None}))
            if not _same_payload(stored, payload_hash):
                return _conflict(
                    "reconciliation", stored, payload_hash,
                    f"execution {execution_id} is already reconciled "
                    f"with a different note or publication consent — a "
                    f"differing payload is a conflict, not a retry, and "
                    f"nothing was written. Publication consent is a "
                    f"third party's decision about their own financial "
                    f"record; changing it is not a reconciliation retry")
            out = _execution_dict(s, row)
            out["idempotent"] = True
            return out
        row.reconciled = True
        row.reconciliation_note = note
        if publication_consent is not None:
            row.publication_consent = bool(publication_consent)
        row.reconciliation_payload_hash = payload_hash
        s.commit()
        return _execution_dict(s, row)
    finally:
        s.close()


def _mid(quote) -> Decimal | None:
    """Mid of a quote in exact dollars, or None."""
    if quote is None:
        return None
    from src.live import risk
    ask = risk._exact_price(quote, "yes_ask")
    bid = risk._exact_price(quote, "yes_bid")
    if ask is None or bid is None:
        return ask if ask is not None else bid
    return (ask + bid) / 2


def gaps_for(s, bet: PersonalBet, ex: PersonalBetExecution) -> dict:
    """The measured gaps for one execution.

    Slippage is DECOMPOSED. `fill - stated` alone is uninterpretable:
    several different things produce the same number, and only some of
    them say anything about the execution model.

        aggressiveness  stated   - mid@record   was the quoted price
                                                realistic to begin with?
        market_drift    mid@fill - mid@record   latency's real cost
        execution_cost  fill     - mid@fill     is the fee/spread model
                                                right?

    These satisfy an exact identity, which `slippage_identity_holds`
    asserts rather than assumes:

        slippage = market_drift + execution_cost - aggressiveness

    The three-way split matters. A two-way one (drift + execution cost)
    silently attributes an unrealistic stated price to execution, and
    "my quoted price was never achievable" is a different lesson from
    "the exchange charged more than modelled".
    """
    out: dict = {"status": ex.status}
    # journal-P0-E: name the EVIDENCE the decomposition rests on. The
    # decomposed metrics (drift / execution cost / aggressiveness) need
    # BOTH books; with either missing they are explicitly absent under
    # this classification, never silently missing.
    #
    # journal-P0-H: and a book that EXISTS is not automatically evidence.
    # Kalshi publishes no quote timestamp, so a capture far from the
    # moment it is claimed to describe is not "the book at the time".
    # Present-but-stale gets its own classification rather than being
    # promoted into `record_and_fill_books` — this is the READ side of
    # the ceiling, and it is what catches rows recorded before the
    # ceiling existed.
    has_rec = bet.market_quote_id is not None
    has_fill = ex.market_quote_id_at_fill is not None
    rec_fresh = _quote_freshness(bet)
    fill_fresh = _fill_quote_freshness(s, ex)
    if has_rec and has_fill:
        rec_ok, fill_ok = rec_fresh == "fresh", fill_fresh == "fresh"
        out["gaps_basis"] = (
            "record_and_fill_books" if rec_ok and fill_ok
            else "stale_books" if not rec_ok and not fill_ok
            else "stale_record_book" if not rec_ok
            else "stale_fill_book")
    else:
        out["gaps_basis"] = (
            "no_fill_book" if has_rec
            else "no_record_book" if has_fill
            else "no_books")
    if has_rec:
        out["record_quote_age_seconds"] = (
            bet.quote_age_seconds
            if bet.quote_age_seconds is not None
            else _age_seconds(bet.quote_observed_at, bet.recorded_at))
        out["record_quote_freshness"] = rec_fresh
    if has_fill:
        out["fill_quote_age_seconds"] = (
            ex.fill_quote_age_seconds
            if ex.fill_quote_age_seconds is not None
            else _age_seconds(
                getattr(s.get(MarketQuote, ex.market_quote_id_at_fill),
                        "captured_at", None), ex.filled_at))
        out["fill_quote_freshness"] = fill_fresh
    stated = _d(bet.stated_price_dollars)
    fill = _d(ex.fill_price_dollars)
    if fill is not None and stated is not None:
        out["slippage"] = str(fill - stated)
    if ex.filled_at and bet.recorded_at:
        out["latency_seconds"] = int(
            (_utc(ex.filled_at) - _utc(bet.recorded_at)).total_seconds())
    if out["gaps_basis"] == "record_and_fill_books":
        mid_rec = _mid(s.get(MarketQuote, bet.market_quote_id))
        mid_fill = _mid(s.get(MarketQuote, ex.market_quote_id_at_fill))
        if mid_rec is not None and mid_fill is not None:
            out["market_drift"] = str(mid_fill - mid_rec)
        if fill is not None and mid_fill is not None:
            out["execution_cost"] = str(fill - mid_fill)
        if stated is not None and mid_rec is not None:
            out["aggressiveness"] = str(stated - mid_rec)
    # the identity is asserted, not assumed — if it ever fails, one of
    # the components is measured against the wrong reference
    if all(k in out for k in ("slippage", "market_drift",
                             "execution_cost", "aggressiveness")):
        lhs = Decimal(out["slippage"])
        rhs = (Decimal(out["market_drift"])
               + Decimal(out["execution_cost"])
               - Decimal(out["aggressiveness"]))
        out["slippage_identity_holds"] = (lhs == rhs)
    # fee_delta: the model is general-taker-only and explicitly
    # approximate. Where it and the exchange disagree, the exchange wins
    # and the difference is recorded, never smoothed away.
    paid = _d(ex.fee_paid_dollars)
    qty = _d(ex.filled_contracts)
    if paid is not None and fill is not None and qty is not None:
        from src.live.paper import order_fee_dollars
        modelled = order_fee_dollars(fill, qty)
        out["fee_modelled"] = str(modelled)
        out["fee_delta"] = str(paid - modelled)
    credit = _d(ex.settlement_credit_dollars)
    if credit is not None and qty is not None and bet.settled_outcome:
        expected = qty if bet.settled_outcome == bet.outcome_key \
            else Decimal(0)
        out["settlement_delta"] = str(credit - expected)
    return out


def _safe_gaps(s, bet, row) -> dict:
    """gaps_for, guaranteed not to raise (journal-P0-D): the execution
    row is already committed when the response is assembled, so a
    derived-metric failure must degrade to an explicit marker — never
    surface as a 500 that makes a recorded fill look unrecorded."""
    if bet is None:
        return {}
    try:
        return gaps_for(s, bet, row)
    except Exception as exc:
        print(f"[journal] gap computation failed for execution "
              f"{row.id}: {exc}")
        return {"gaps_computation_error": str(exc)[:160]}


def _superseded_ids(s, bet_ids) -> set:
    """The ids among `bet_ids` that a later entry corrects
    (journal-P1-F). A superseded row remains in the record — it is the
    immutable audit trail of the mistake — but it contributes no
    effective observation to any count; its correction does."""
    ids = list(bet_ids)
    if not ids:
        return set()
    rows = (s.query(PersonalBet.corrects_bet_id)
            .filter(PersonalBet.corrects_bet_id.in_(ids)).all())
    return {r[0] for r in rows}


def _bet_dict(row: PersonalBet, *, superseded: bool = False) -> dict:
    return {
        "id": row.id, "fixture_id": row.fixture_id,
        "market_ticker": row.market_ticker,
        "outcome_key": row.outcome_key, "status": row.status,
        "stated_price_dollars": row.stated_price_dollars,
        "stated_size": row.stated_size,
        "price_basis": row.price_basis,
        "market_quote_id": row.market_quote_id,
        "recorded_at": (row.recorded_at.isoformat()
                        if row.recorded_at else None),
        "resolved_at": (row.resolved_at.isoformat()
                        if row.resolved_at else None),
        "rationale": row.rationale,
        "model_probability": row.model_probability,
        "prediction_run_id": row.prediction_run_id,
        "settled_outcome": row.settled_outcome,
        "content_hash": row.content_hash,
        "corrects_bet_id": row.corrects_bet_id,
        # journal-P0-H: the capture age of the cited book, the ceiling
        # applied, and — when a citation was refused — why. Reported, so
        # a reader can check the classification instead of trusting it.
        "quote_age_seconds": row.quote_age_seconds,
        "quote_max_age_seconds": row.quote_max_age_seconds,
        "quote_rejection_reason": row.quote_rejection_reason,
        "quote_freshness": _quote_freshness(row),
        # journal-P1-F: a corrected row is audit, not an observation
        "superseded": superseded,
        "counts_toward_aggregate": _counts_toward_aggregate(row,
                                                            superseded),
    }


def _execution_dict(s, row: PersonalBetExecution) -> dict:
    bet = s.get(PersonalBet, row.personal_bet_id)
    return {
        "id": row.id, "personal_bet_id": row.personal_bet_id,
        "account_label": row.account_label, "status": row.status,
        # the consent provenance the operator surface documents itself
        # as serving. It was required at write, stored, and then
        # readable through NO api — a record nobody can read is not
        # provenance, it is a promise.
        "consent_recorded_at": _iso(row.consent_recorded_at),
        "not_filled_reason": row.not_filled_reason,
        "best_available_price_dollars": row.best_available_price_dollars,
        "fill_price_dollars": row.fill_price_dollars,
        "filled_contracts": row.filled_contracts,
        "fee_paid_dollars": row.fee_paid_dollars,
        "filled_at": row.filled_at.isoformat() if row.filled_at else None,
        "exchange_order_id": row.exchange_order_id,
        "settlement_credit_dollars": row.settlement_credit_dollars,
        "settled_at": (row.settled_at.isoformat()
                       if row.settled_at else None),
        "reconciled": bool(row.reconciled),
        "reconciliation_note": row.reconciliation_note,
        "publication_consent": bool(row.publication_consent),
        # journal-P0-H / P0-I: the evidence the server verified, so an
        # operator can audit the acceptance rather than trust it
        "fill_quote_age_seconds": row.fill_quote_age_seconds,
        "fill_quote_max_age_seconds": row.fill_quote_max_age_seconds,
        "execution_payload_hash": row.execution_payload_hash,
        "settlement_payload_hash": row.settlement_payload_hash,
        "reconciliation_payload_hash": row.reconciliation_payload_hash,
        "gaps": _safe_gaps(s, bet, row),
    }


def journal_summary(fixture_id: int | None = None,
                    competition_slug: str | None = "mls-2026") -> dict:
    """The journal, with its DENOMINATOR.

    A hit rate over taken bets alone is not a statistic — the passes are
    what make the taken ones interpretable, exactly as `paper_coverage`
    had to exist before a signal count could be read.

    Below MIN_EXECUTIONS_FOR_AGGREGATE this returns the rows and NO
    summary statistic. Not a zero: absent. Three fills tell you the fee
    model is not OBVIOUSLY broken, which is not the same as telling you
    it is right.

    Scoped to one competition by default (journal-P1 F9): the MLS
    surface reports the MLS journal, and a second league's entries do
    not inflate its denominator. Pass None for the cross-competition
    view (internal use only).
    """
    if not plane_ready():
        return {}
    s = get_session()
    try:
        q = s.query(PersonalBet)
        if competition_slug is not None:
            q = q.filter_by(competition_slug=competition_slug)
        if fixture_id is not None:
            q = q.filter_by(fixture_id=fixture_id)
        bets = q.all()
        # journal-P1-F: one effective observation per correction chain.
        # A corrected row stays in the record (and in total_recorded —
        # the audit denominator) but contributes to NO aggregate; its
        # correction does. Corrections share their target's scope by
        # construction, so the corrector is always inside this query's
        # scope.
        superseded_ids = {b.corrects_bet_id for b in bets
                          if b.corrects_bet_id is not None}
        effective = [b for b in bets if b.id not in superseded_ids]
        counts = {k: 0 for k in STATUSES}
        for b in effective:
            counts[b.status] = counts.get(b.status, 0) + 1
        # journal-P0-H: `countable` uses the SAME predicate the row-level
        # surfaces use, so an entry citing a stale book cannot be
        # excluded per-row and still counted in the denominator here.
        countable = [b for b in effective
                     if _counts_toward_aggregate(b, superseded=False)]
        stale_quote = [b for b in effective
                       if _quote_freshness(b) == "stale"]
        ids = {b.id for b in bets}
        execs = (s.query(PersonalBetExecution)
                 .filter(PersonalBetExecution.personal_bet_id.in_(ids)).all()
                 if ids else [])
        settled = [e for e in execs
                   if e.status in ("filled", "partial") and e.settled_at]
        out = {
            "policy": JOURNAL_POLICY,
            "evidence_class": "personal_journal",
            "counts": counts,
            "total_recorded": len(bets),
            # journal-P1-F: the counting surfaces above are EFFECTIVE
            # (one observation per correction chain); the raw rows are
            # all still present and total_recorded still counts them
            "effective_recorded": len(effective),
            "superseded": len(bets) - len(effective),
            "countable": len(countable),
            "excluded_stated_only": sum(
                1 for b in effective if b.price_basis == "stated_only"),
            # journal-P0-H: entries whose cited book is outside the
            # capture-age ceiling. Reported as its own number rather
            # than folded into stated_only — "we had no price" and "the
            # price we had was too old to be the book" are different
            # findings, and only the second says something about the
            # capture pipeline.
            "excluded_stale_quote": len(stale_quote),
            "quote_max_age_seconds": int(
                config.JOURNAL_QUOTE_MAX_AGE_SECONDS),
            "executions": {
                "total": len(execs),
                "filled": sum(1 for e in execs if e.status == "filled"),
                "partial": sum(1 for e in execs if e.status == "partial"),
                "not_filled": sum(1 for e in execs
                                  if e.status == "not_filled"),
                "settled": len(settled),
            },
            "note": ("execution-fidelity evidence. NEVER edge evidence — "
                     "these bets are human-selected. Never sums with the "
                     "paper ledger."),
        }
        # journal-P1 F7: the marker NEVER silently disappears. Below the
        # minimum it explains the floor; at or above it, it states that
        # aggregation is deliberately not implemented — crossing n=20
        # must not quietly convert a floor into an implied green light
        # for whatever summary a reader wants to compute.
        if len(settled) < MIN_EXECUTIONS_FOR_AGGREGATE:
            out["aggregate_withheld"] = {
                "reason": "below_minimum_observations",
                "settled_executions": len(settled),
                "minimum": MIN_EXECUTIONS_FOR_AGGREGATE,
                "note": ("rows are listed; no mean, rate or ROI is "
                         "reported. A summary over this many "
                         "observations would be a narrative."),
            }
        else:
            out["aggregate_withheld"] = {
                "reason": ("aggregation deliberately not implemented — "
                           "pre-specified metrics not yet defined"),
                "settled_executions": len(settled),
                "minimum": MIN_EXECUTIONS_FOR_AGGREGATE,
                "note": ("the observation floor is met, but no metric "
                         "was pre-specified before the data arrived. "
                         "Defining one now, after seeing the rows, "
                         "would be exactly the post-hoc selection this "
                         "journal exists to avoid."),
            }
        return out
    finally:
        s.close()


def journal_scopes(prefix: str) -> list[str]:
    """Competition slugs under `prefix` that the journal actually holds.

    The viewer surfaces write rows with a caller-supplied slug
    (`leagues-cup-2026`), and until now nothing could READ them: the one
    public journal route is pinned to `mls-2026` by journal-P1 F9, so a
    viewer row was writable and invisible. Discovering the slugs rather
    than deriving them from a key plus a season keeps that fix from
    inventing a denominator — `Viewer.season_default` is None for most
    competitions and the provider's season label is not always the
    calendar year, so a derived slug would silently report an empty
    journal for rows that exist under a neighbouring one.

    Returns what is there, sorted. Never pools: each slug is its own
    denominator, which is the whole point of F9.
    """
    if not plane_ready():
        return []
    s = get_session()
    try:
        rows = (s.query(PersonalBet.competition_slug)
                .filter(PersonalBet.competition_slug.like(f"{prefix}-%"))
                .distinct().all())
        return sorted(r[0] for r in rows if r[0])
    finally:
        s.close()


# --- the broadcast boundary (journal-P0 F1) --------------------------------
# The action channel relays prose to Son, whose consenting friend places
# REAL bets on the same views. The real-money lock
# (REAL_MONEY_SIGNALS_ENABLED=false) governs MODEL-GENERATED signals;
# operator-authenticated, session-sourced prose relayed to Son is
# documentation, not a model signal — the carve-out recorded in
# AGENTS.md §3, PENDING SON'S SIGN-OFF AT MERGE. The boundary is
# enforced HERE, mechanically, not by caller honesty:
#
#   - only source="session" may dispatch; any other source is refused
#   - a call whose stack contains a scheduler or model frame is refused
#     even when it CLAIMS source="session"
#   - the caller must present a CAPABILITY minted by the interactive-
#     session handshake (journal-P0-G). The stack check binds only
#     in-process callers: over HTTP the stack is uvicorn → fastapi →
#     api.main and contains no guarded frame, so any holder of the
#     generic operator token could claim source="session" and dispatch.
#     A capability is a server-side artifact ADMIN_TOKEN alone cannot
#     produce — see src/live/session_capability.py
#   - every action-channel dispatch carries the standing-edge qualifier
#     appended server-side, so no relayed number can travel without its
#     uncertainty
_AUTOMATED_CALLER_PREFIXES = (
    "apscheduler", "jobs",
    "src.live.runs", "src.live.paper", "src.live.ingest",
    "src.live.markets", "src.live.model_mls", "src.live.model_eval",
    "src.live.slate", "src.live.risk", "src.live.mls_stats",
    "src.live.lineups", "src.live.audit", "src.live.settlement",
)


def _automated_caller() -> str | None:
    """The first scheduler/model module on the call stack, or None.

    A parameter can be forged by any caller; the stack cannot. This is
    the server-side check that makes `source="session"` a verified fact
    rather than caller honesty."""
    import inspect
    for fi in inspect.stack(0)[1:]:
        mod = fi.frame.f_globals.get("__name__") or ""
        if mod == __name__:
            continue
        if any(mod == p or mod.startswith(p + ".")
               for p in _AUTOMATED_CALLER_PREFIXES):
            return mod
    return None


def _shadow_qualifier() -> str:
    """The standing edge WITH its interval and significance flag, read
    fresh — appended server-side to every action-channel dispatch so no
    relayed message can present a number without its uncertainty."""
    try:
        from src.live import model_eval
        d = model_eval.current_approval_decision()
        edge = d.get("edge_vs_baseline")
        lo, hi = d.get("ci_low"), d.get("ci_high")
        if edge is not None and lo is not None and hi is not None:
            sig = ("significant" if d.get("edge_significant")
                   else "not significant")
            return (f"[shadow] standing edge {edge:+.4f}, "
                    f"n={d.get('n_scored')}, CI [{lo:+.4f}, {hi:+.4f}] — "
                    f"{sig}; shadow model, not a real-money signal")
    except Exception as exc:
        print(f"[journal] qualifier read failed: {exc}")
    return ("[shadow] no standing-edge decision available — no "
            "established edge; shadow model, not a real-money signal")


def broadcast(message: str, *, channel: str = "action",
              source: str = "session", session_label: str | None = None,
              fixture_id: int | None = None,
              claims: dict | None = None,
              capability=None) -> dict:
    """Push a message to Discord/ntfy AND persist what was said.

    The live session is the analyser; this is its megaphone. Persisting
    every broadcast means a session that dropped mid-match can read what
    it already said instead of repeating or contradicting itself — and
    what was claimed live, when, is documentation in the same sense the
    journal is.

    Only operator-authenticated SESSION prose may dispatch — see the
    boundary note above. A `computed` source names a model-generated
    message, which is exactly what the real-money lock governs, so it is
    refused here rather than relayed; the refusal is logged, never
    silently dropped. `dispatched` is true only when at least one
    transport ACCEPTED the message (journal-P1 F6), and the accepting
    transports are named.

    `capability` must be a live `SessionCapability` minted by the
    interactive-session handshake (journal-P0-G). It is re-checked
    against the server-side store, so neither a forged parameter nor a
    hand-built object passes — and a caller holding only the generic
    operator token has no way to obtain one. Every refusal below returns
    BEFORE any row is written: a refused broadcast is not "said".
    """
    if not plane_ready():
        return {"error": "dormant"}
    if channel not in ("action", "detail"):
        return {"error": "channel must be action or detail"}
    if source not in ("session", "computed"):
        return {"error": "source must be session or computed"}
    if source != "session":
        print(f"[journal] broadcast REFUSED: source={source!r} is a "
              f"model-generated signal and REAL_MONEY_SIGNALS_ENABLED="
              f"{config.REAL_MONEY_SIGNALS_ENABLED} — no dispatch path")
        return {"refused": True, "dispatched": False,
                "error": ("refused: only source=\"session\" "
                          "(operator-authenticated prose) may dispatch. "
                          "A computed message is a model-generated "
                          "signal; the real-money lock provides no "
                          "dispatch path for those")}
    auto = _automated_caller()
    if auto is not None:
        print(f"[journal] broadcast REFUSED: automated caller {auto} "
              f"claimed source=session")
        return {"refused": True, "dispatched": False,
                "error": (f"refused: broadcast() was reached from {auto} "
                          f"— scheduled jobs and model code paths may "
                          f"never dispatch, whatever source they claim")}
    # journal-P0-G: source="session" must be a server-VERIFIABLE fact,
    # not a claim. The stack check above binds in-process callers only;
    # this is what binds the HTTP path.
    from src.live import session_capability
    cap = session_capability.is_live(capability)
    if cap is None:
        print("[journal] broadcast REFUSED: no live interactive-session "
              "capability was presented")
        return {"refused": True, "dispatched": False,
                "error": ("refused: action dispatch requires a live "
                          "interactive-session capability. The operator "
                          "token authenticates the request; it does not "
                          "establish that a human session is speaking. "
                          "Open one via POST /api/admin/mls/session/"
                          "challenge then /session/open, and present the "
                          "X-Session-Token header")}
    session_label = session_label or cap.label
    # --- compose the wire payload WITHIN the transport limit ----------
    # journal-P0-A: the transports truncate at TRANSPORT_MESSAGE_LIMIT,
    # and on a long action message the tail they cut was exactly the
    # qualifier. So the qualifier's space is RESERVED first and the
    # operator prose is truncated (with an explicit marker) to fit —
    # the uncertainty language is the one part that must never shed.
    from src import alerts
    limit = alerts.TRANSPORT_MESSAGE_LIMIT
    qualifier = _shadow_qualifier() if channel == "action" else None
    prefix = "🗣 "
    reserved = len(prefix) + (len(qualifier) + 1 if qualifier else 0)
    room = limit - reserved
    if room <= 0:
        return {"error": "transport limit cannot fit the qualifier — "
                         "refusing to dispatch a number without its "
                         "uncertainty"}
    prose, truncated = message, False
    if len(prose) > room:
        marker = " …[truncated; full prose in the journal record]"
        prose = prose[:room - len(marker)] + marker
        truncated = True
    body = f"{prefix}{prose}" + (f"\n{qualifier}" if qualifier else "")
    assert len(body) <= limit, "composer must respect the wire limit"
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

    s = get_session()
    try:
        row = BroadcastLog(
            fixture_id=fixture_id, channel=channel, source=source,
            session_label=session_label, message=message,
            claims_json=(json.dumps(claims, sort_keys=True, default=str)
                         if claims else None),
            created_at=_now(),
            # the wire record: what is actually being handed to the
            # transports, not what the caller composed
            dispatched_body=body, dispatched_sha256=body_sha,
            # journal-P0-G: WHICH authorised session said it. The gate is
            # useless as evidence if the record cannot name the origin it
            # verified.
            dispatch_capability_id=cap.id)
        s.add(row)
        s.flush()
        transports: dict = {}
        try:
            # SESSION_RELAY: the carve-out, declared at the call site.
            # The capability travels WITH the dispatch so the gate
            # re-verifies it against the server-side store rather than
            # trusting that this function checked it above — the gate is
            # the boundary, and a boundary that trusts its callers is a
            # convention.
            result = alerts.send_alert(body, title="Trivela",
                                       kind=channel,
                                       dispatch_class=alerts.SESSION_RELAY,
                                       capability=cap)
            if isinstance(result, dict):
                transports = result
        except Exception as exc:      # a failed send must not lose the record
            print(f"[journal] broadcast dispatch failed: {exc}")
        accepted = sorted(k for k, v in transports.items() if v)
        # true only when at least one transport ACCEPTED the message —
        # "dispatched without raising" is not delivery (journal-P1 F6)
        dispatched = bool(accepted)
        row.delivered = dispatched
        row.transports_json = json.dumps(transports, sort_keys=True)
        s.commit()
        return {"id": row.id, "dispatched": dispatched,
                "transports": transports, "accepted": accepted,
                "payload_truncated": truncated,
                "dispatched_sha256": body_sha,
                "dispatch_capability_id": cap.id,
                "channel": channel, "source": source}
    finally:
        s.close()


def recent_broadcasts(fixture_id: int, limit: int = 20) -> list[dict]:
    """What has already been said about this fixture, newest first — so a
    reopened session picks up the thread rather than contradicting what
    it said twenty minutes ago."""
    if not plane_ready():
        return []
    s = get_session()
    try:
        rows = (s.query(BroadcastLog).filter_by(fixture_id=fixture_id)
                .order_by(BroadcastLog.id.desc()).limit(limit).all())
        return [{"id": r.id, "channel": r.channel, "source": r.source,
                 "session_label": r.session_label, "message": r.message,
                 "claims": (json.loads(r.claims_json)
                            if r.claims_json else None),
                 "delivered": bool(r.delivered),
                 # the wire record (journal-P0-A): what was actually
                 # sent, verifiable against its hash
                 "dispatched_body": r.dispatched_body,
                 "dispatched_sha256": r.dispatched_sha256,
                 # journal-P0-G: the verified origin of the dispatch
                 "dispatch_capability_id": r.dispatch_capability_id,
                 "transports": (json.loads(r.transports_json)
                                if r.transports_json else None),
                 "created_at": (r.created_at.isoformat()
                                if r.created_at else None)}
                for r in rows]
    finally:
        s.close()


# --- THE public projection (journal-P0 F2 + P0-C) --------------------------
# The briefing and journal endpoints are UNAUTHENTICATED reads, and an
# execution row is a third party's financial record. There is exactly
# ONE public projection of a journal entry, used by the briefing, the
# journal surface and the corpus — one field list, so the surfaces
# cannot drift apart (round-1 shipped two lists and they already
# differed). The view and its resolution travel; the person and the
# money never do:
#   - no rationale (private prose — no consent field exists for it)
#   - no stated_size (position sizing is third-party financial detail)
#   - executions appear on public surfaces as COUNTS only, and in the
#     corpus only under the row's explicit publication_consent
#
# NOTE this list DOCUMENTS the boundary; it does not enforce it. The
# enforcement is structural — public surfaces call
# `public_bet_projection` and an execution count, and never
# `_execution_dict`. Keep it accurate anyway: a stale list beside a
# privacy boundary reads as a guarantee to the next person.
_PRIVATE_EXECUTION_FIELDS = frozenset({
    "account_label", "consent_recorded_at", "exchange_order_id",
    "fill_price_dollars", "filled_contracts", "fee_paid_dollars",
    "best_available_price_dollars", "settlement_credit_dollars",
    "reconciliation_note", "gaps", "publication_consent",
    # round 3: the fill-book age and the payload hashes are derived from
    # the private economics, so they belong on this side of the line too
    "fill_quote_age_seconds", "fill_quote_max_age_seconds",
    "execution_payload_hash", "settlement_payload_hash",
    "reconciliation_payload_hash"})


def _iso(dt):
    return dt.isoformat() if dt else None


def public_bet_projection(row: PersonalBet, *,
                          superseded: bool = False) -> dict:
    """The single public shape of a journal entry. Every public surface
    (briefing, journal, corpus) serves THIS and nothing wider.

    `superseded` is caller-computed (journal-P1-F): whether a later
    entry corrects this one is a fact about the SET being served, not
    about the row, so the surface assembling the set supplies it."""
    return {
        "id": row.id,
        "competition_slug": row.competition_slug,
        "fixture_id": row.fixture_id,
        "market_ticker": row.market_ticker,
        "market_contract_id": row.market_contract_id,
        "outcome_key": row.outcome_key,
        "status": row.status,
        "stated_price_dollars": row.stated_price_dollars,
        "price_basis": row.price_basis,
        "market_quote_id": row.market_quote_id,
        "quote_observed_at": _iso(row.quote_observed_at),
        "recorded_at": _iso(row.recorded_at),
        "resolved_at": _iso(row.resolved_at),
        "model_probability": row.model_probability,
        "prediction_run_id": row.prediction_run_id,
        "settled_outcome": row.settled_outcome,
        "settled_at": _iso(row.settled_at),
        "content_hash": row.content_hash,
        "corrects_bet_id": row.corrects_bet_id,
        # journal-P0-H: capture age, applied ceiling and refusal reason
        # are PUBLIC — the whole point of citing a quote is that a
        # reader can check it, and they cannot check a book whose age
        # they are not told
        "quote_age_seconds": row.quote_age_seconds,
        "quote_max_age_seconds": row.quote_max_age_seconds,
        "quote_rejection_reason": row.quote_rejection_reason,
        "quote_freshness": _quote_freshness(row),
        # journal-P1-F: a corrected row is audit, not an observation
        "superseded": superseded,
        "counts_toward_aggregate": _counts_toward_aggregate(row,
                                                            superseded),
    }


def open_entries(s, fixture_id: int, redact: bool = True) -> list[dict]:
    """Journal entries on this fixture that still matter to a live
    reader: everything except void. Redacted by default — this feeds
    the PUBLIC briefing (journal-P0 F2/P0-C), which serves THE public
    projection and execution COUNTS only: even an execution's
    occurrence/timeline is third-party-linkable metadata. Only an
    operator-authenticated caller may pass redact=False."""
    rows = (s.query(PersonalBet).filter_by(fixture_id=fixture_id)
            .filter(PersonalBet.status != "void")
            .order_by(PersonalBet.id.desc()).all())
    superseded = _superseded_ids(s, (b.id for b in rows))
    out = []
    for b in rows:
        execs = (s.query(PersonalBetExecution)
                 .filter_by(personal_bet_id=b.id).all())
        if redact:
            d = public_bet_projection(b, superseded=b.id in superseded)
            d["executions"] = {"count": len(execs)}
        else:
            d = _bet_dict(b, superseded=b.id in superseded)
            d["executions"] = [_execution_dict(s, e) for e in execs]
        out.append(d)
    return out


def full_entries(fixture_id: int | None = None) -> list[dict]:
    """The COMPLETE journal record — every entry (void included), every
    execution with its economics, gaps and reconciliation state. This is
    the operator surface (journal-P0 F2); it must only ever be served
    behind operator authentication."""
    if not plane_ready():
        return []
    s = get_session()
    try:
        q = s.query(PersonalBet)
        if fixture_id is not None:
            q = q.filter_by(fixture_id=fixture_id)
        rows = q.order_by(PersonalBet.id.desc()).all()
        superseded = _superseded_ids(s, (b.id for b in rows))
        out = []
        for b in rows:
            d = _bet_dict(b, superseded=b.id in superseded)
            d["executions"] = [
                _execution_dict(s, e) for e in
                s.query(PersonalBetExecution)
                .filter_by(personal_bet_id=b.id).all()]
            out.append(d)
        return out
    finally:
        s.close()


def briefing(espn_event_id: str) -> dict:
    """Everything needed to reason about one fixture RIGHT NOW, in one
    call.

    Built for a live session, not a human. A session opening cold
    mid-match otherwise spends its first turns assembling state from
    several endpoints while the match moves.

    Three deliberate properties:

    - **Frozen and current sit side by side, always labelled.** The T-10
      book and the live book are different evidence classes; showing one
      without the other invites exactly the wrong comparison.
    - **The standing edge travels WITH its interval and significance
      flag**, so a consumer cannot quote the number without the
      qualifier attached. Same rule the UI follows, applied to the
      machine surface.
    - **`said_already`** carries what has already been broadcast about
      this fixture, so a session that dropped and reopened picks up the
      thread instead of contradicting itself.
    """
    if not plane_ready():
        return {"error": "dormant"}
    out: dict = {"espn_event_id": espn_event_id,
                 "generated_at": _now().isoformat()}

    # --- the match itself, and the CURRENT book -------------------------
    try:
        from src import mls
        summary = mls.match_summary(espn_event_id)
        out["fixture"] = summary
        if summary:
            # journal-P0 F4: the read carries its own age and a truthful
            # status. `basis` says live_read ONLY when the read is live;
            # a failed refresh serves the cache AS a labelled fallback,
            # and past MLS_PRICE_MAX_AGE_SECONDS it fails closed — no
            # price is presented as current at all.
            books, fresh = mls.find_all_books_with_freshness(
                summary.get("date"),
                (summary.get("home") or {}).get("name") or "",
                (summary.get("away") or {}).get("name") or "")
            status = fresh.get("status")
            out["market_current"] = {
                "basis": {"live": "live_read",
                          "stale_fallback": "cached_read"}.get(
                              status, "unavailable"),
                "status": status,
                "observed_at": fresh.get("observed_at"),
                "age_seconds": fresh.get("age_seconds"),
                "books": books if status != "unavailable" else [],
            }
            if status == "stale_fallback":
                out["market_current"]["note"] = (
                    "NOT a live read — refresh failed; serving the last "
                    "observation WITH its age. Treat prices accordingly")
            elif status != "live":
                out["market_current"]["note"] = (
                    "refresh failed and the last observation is beyond "
                    "MLS_PRICE_MAX_AGE_SECONDS — no current price is "
                    "presented")
    except Exception as exc:
        out["fixture_error"] = str(exc)[:160]

    # --- the model, and the FROZEN T-10 book ---------------------------
    try:
        from src.live import runs as live_runs
        out["model"] = live_runs.model_for_event(espn_event_id)
    except Exception as exc:
        out["model_error"] = str(exc)[:160]

    s = get_session()
    try:
        fx = (s.query(Fixture)
              .filter_by(espn_event_id=str(espn_event_id)).first())
        if fx is None:
            out["note"] = "fixture not in the live plane"
            return out
        out["fixture_id"] = fx.id

        # the frozen lock book, labelled as a DIFFERENT class from above
        from src.live.models import (MarketContract, PredictionContract,
                                     PredictionRun)
        lock = (s.query(PredictionRun)
                .filter_by(fixture_id=fx.id, run_type="t10",
                           canonical=True, status="complete").first())
        if lock is not None:
            frozen = []
            for c in (s.query(PredictionContract)
                      .filter_by(prediction_run_id=lock.id).all()):
                q = (s.get(MarketQuote, c.market_quote_id)
                     if c.market_quote_id else None)
                mc = (s.get(MarketContract, c.market_contract_id)
                      if c.market_contract_id else None)
                frozen.append({
                    "outcome_key": c.outcome_key,
                    "ticker": mc.ticker if mc else None,
                    "model_probability": c.raw_probability,
                    # the persisted id, so the documented record-view
                    # flow can CITE this quote (journal-P0 F3)
                    "market_quote_id": c.market_quote_id,
                    "frozen_yes_ask_dollars": (
                        q.yes_ask_dollars if q else None),
                    "frozen_yes_bid_dollars": (
                        q.yes_bid_dollars if q else None),
                    "quote_captured_at": (q.captured_at.isoformat()
                                          if q and q.captured_at else None),
                })
            out["market_frozen_t10"] = {
                "basis": "frozen_at_lock",
                "run_id": lock.id,
                "captured_at": (lock.captured_at.isoformat()
                                if lock.captured_at else None),
                "contracts": frozen,
                "note": ("the book as it stood at T-10. Compare to "
                         "market_current deliberately, never accidentally"),
            }

        # --- citable persisted quotes (journal-P0 F3 + F4) -------------
        # The newest STORED quote per contract, with its id, capture
        # time, age and a truthful status — so the documented flow
        # ("cite market_quote_id from the briefing") can actually
        # produce an id the server will accept, and no stored price is
        # presented as current past the TTL.
        from src.live.models import MarketEvent
        now = _now()
        max_age = config.MLS_PRICE_MAX_AGE_SECONDS
        persisted = []
        for me in (s.query(MarketEvent)
                   .filter_by(fixture_id=fx.id, mapping_approved=True)
                   .all()):
            for mc in (s.query(MarketContract)
                       .filter_by(market_event_id=me.id).all()):
                q = (s.query(MarketQuote)
                     .filter_by(market_contract_id=mc.id)
                     .order_by(MarketQuote.captured_at.desc(),
                               MarketQuote.id.desc()).first())
                if q is None or q.captured_at is None:
                    continue
                age = int((now - _utc(q.captured_at)).total_seconds())
                q_status = ("live" if age <= 60 else "stale_fallback"
                            if age <= max_age else "unavailable")
                entry = {"market_quote_id": q.id,
                         "market_contract_id": mc.id,
                         "ticker": mc.ticker,
                         "outcome_key": mc.outcome_key,
                         "captured_at": _utc(q.captured_at).isoformat(),
                         "age_seconds": age,
                         "status": q_status}
                if q_status != "unavailable":
                    entry["yes_ask_dollars"] = q.yes_ask_dollars
                    entry["yes_bid_dollars"] = q.yes_bid_dollars
                persisted.append(entry)
        out["market_persisted"] = {
            "basis": "persisted_observation",
            "note": ("cite market_quote_id from here or from "
                     "market_frozen_t10 when recording a view — these "
                     "ids are server-verified against this fixture. "
                     "Prices past the TTL are withheld; the id and "
                     "capture time remain citable"),
            "quotes": persisted,
        }

        out["journal"] = open_entries(s, fx.id)
        # journal-P0-C: broadcast PROSE is operator content — the ops
        # prompt fills it with fill announcements — so the public
        # briefing carries only the count. The session reads the thread
        # from the authenticated GET /api/admin/mls/broadcasts.
        out["said_already"] = {
            "count": s.query(BroadcastLog)
                      .filter_by(fixture_id=fx.id).count(),
            "note": ("broadcast content is operator-authenticated — "
                     "GET /api/admin/mls/broadcasts?fixture_id=..."),
        }
    finally:
        s.close()

    # --- the standing edge, WITH its uncertainty ------------------------
    try:
        from src.live import model_eval
        d = model_eval.current_approval_decision()
        out["context"] = {
            "edge_vs_baseline": d.get("edge_vs_baseline"),
            "ci95": [d.get("ci_low"), d.get("ci_high")],
            "significant": d.get("edge_significant"),
            "n_scored": d.get("n_scored"),
            "model_version": d.get("model_version"),
            "note": ("the interval travels with the estimate. An edge "
                     "whose CI spans zero is not an established edge — "
                     "do not report the point estimate alone"),
        }
    except Exception as exc:
        out["context_error"] = str(exc)[:160]

    out["evidence_note"] = (
        "shadow model, observational. Journal entries are human-selected "
        "and are NOT edge evidence. Never sum journal, paper ledger and "
        "real executions.")
    return out
