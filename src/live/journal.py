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
from datetime import datetime, timezone
from decimal import Decimal

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
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _content_hash(doc: dict) -> str:
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def record_view(fixture_id: int, market_ticker: str, *,
                outcome_key: str | None = None,
                stated_price=None, stated_size=None,
                rationale: str | None = None,
                market_quote_id: int | None = None,
                market_contract_id: int | None = None,
                status: str = "considered") -> dict:
    """Record a view at the moment it FORMS.

    Returns the stored row as a dict, including the price_basis actually
    granted — which may be weaker than the caller hoped. The rules below
    are enforced here rather than trusted to callers:

    - `observed_quote` is granted only when a real quote exists and was
      captured at or before now. Otherwise the entry is `stated_only`,
      recorded honestly and excluded from every aggregate.
    - recording after kickoff yields `void`: keepable, never counted.
    - the model's probability is FROZEN now, with the run it came from,
      so a later re-run cannot change what was recorded.
    """
    if not plane_ready():
        return {"error": "dormant"}
    if status not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}
    s = get_session()
    try:
        fx = s.get(Fixture, fixture_id)
        if fx is None:
            return {"error": "no such fixture"}
        now = _now()

        # 1. price basis — falsifiable or explicitly not
        basis, quote_at = "stated_only", None
        if market_quote_id is not None:
            q = s.get(MarketQuote, market_quote_id)
            if q is not None and q.captured_at is not None \
                    and _utc(q.captured_at) <= now:
                basis, quote_at = "observed_quote", _utc(q.captured_at)
            else:
                market_quote_id = None      # refuse to cite what we lack

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
            stated_price_dollars=(str(_d(stated_price))
                                  if _d(stated_price) is not None else None),
            stated_size=(str(_d(stated_size))
                         if _d(stated_size) is not None else None),
            price_basis=basis, market_quote_id=market_quote_id,
            quote_observed_at=quote_at, recorded_at=now,
            rationale=rationale, model_probability=model_p,
            prediction_run_id=run_id)
        row.content_hash = _content_hash({
            "fixture_id": fixture_id, "market_ticker": market_ticker,
            "outcome_key": outcome_key,
            "stated_price": row.stated_price_dollars,
            "stated_size": row.stated_size, "price_basis": basis,
            "market_quote_id": market_quote_id,
            "recorded_at": now.isoformat(),
            "model_probability": model_p, "prediction_run_id": run_id})
        s.add(row)
        s.commit()
        return _bet_dict(row)
    finally:
        s.close()


def resolve_view(bet_id: int, status: str) -> dict:
    """Move a recorded view to `taken` or `passed`. The pass is the half
    that keeps the journal honest, so it is a first-class transition."""
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
        row.status = status
        row.resolved_at = _now()
        s.commit()
        return _bet_dict(row)
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
                     exchange_order_id: str | None = None) -> dict:
    """Record a REAL fill — or a real failure to fill.

    `consent_recorded_at` is required and never defaulted: this is
    someone else's money, and the provenance of that consent belongs in
    the row rather than in someone's memory.

    A `not_filled` row is as valuable as a fill. It is evidence about
    liquidity, which is half of what this pilot exists to measure.
    """
    if not plane_ready():
        return {"error": "dormant"}
    if status not in EXEC_STATUSES:
        return {"error": f"status must be one of {EXEC_STATUSES}"}
    if not consent_recorded_at:
        return {"error": "consent_recorded_at is required — this is a "
                         "third party's money"}
    if not account_label:
        return {"error": "account_label is required"}
    if status == "not_filled" and not not_filled_reason:
        return {"error": "not_filled requires a reason"}
    s = get_session()
    try:
        bet = s.get(PersonalBet, bet_id)
        if bet is None:
            return {"error": "no such entry"}
        row = PersonalBetExecution(
            personal_bet_id=bet_id, account_label=account_label,
            consent_recorded_at=_utc(consent_recorded_at),
            status=status, not_filled_reason=not_filled_reason,
            best_available_price_dollars=(
                str(_d(best_available_price))
                if _d(best_available_price) is not None else None),
            fill_price_dollars=(str(_d(fill_price))
                                if _d(fill_price) is not None else None),
            filled_contracts=(str(_d(filled_contracts))
                              if _d(filled_contracts) is not None else None),
            fee_paid_dollars=(str(_d(fee_paid))
                              if _d(fee_paid) is not None else None),
            filled_at=_utc(filled_at) or (_now() if status != "not_filled"
                                          else None),
            market_quote_id_at_fill=market_quote_id_at_fill,
            exchange_order_id=exchange_order_id)
        s.add(row)
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
    stated = _d(bet.stated_price_dollars)
    fill = _d(ex.fill_price_dollars)
    if fill is not None and stated is not None:
        out["slippage"] = str(fill - stated)
    if ex.filled_at and bet.recorded_at:
        out["latency_seconds"] = int(
            (_utc(ex.filled_at) - _utc(bet.recorded_at)).total_seconds())
    mid_rec = _mid(s.get(MarketQuote, bet.market_quote_id)
                   if bet.market_quote_id else None)
    mid_fill = _mid(s.get(MarketQuote, ex.market_quote_id_at_fill)
                    if ex.market_quote_id_at_fill else None)
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


def _bet_dict(row: PersonalBet) -> dict:
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
        "counts_toward_aggregate": (row.price_basis == "observed_quote"
                                    and row.status != "void"),
    }


def _execution_dict(s, row: PersonalBetExecution) -> dict:
    bet = s.get(PersonalBet, row.personal_bet_id)
    return {
        "id": row.id, "personal_bet_id": row.personal_bet_id,
        "account_label": row.account_label, "status": row.status,
        "not_filled_reason": row.not_filled_reason,
        "best_available_price_dollars": row.best_available_price_dollars,
        "fill_price_dollars": row.fill_price_dollars,
        "filled_contracts": row.filled_contracts,
        "fee_paid_dollars": row.fee_paid_dollars,
        "filled_at": row.filled_at.isoformat() if row.filled_at else None,
        "exchange_order_id": row.exchange_order_id,
        "reconciled": bool(row.reconciled),
        "gaps": gaps_for(s, bet, row) if bet else {},
    }


def journal_summary(fixture_id: int | None = None) -> dict:
    """The journal, with its DENOMINATOR.

    A hit rate over taken bets alone is not a statistic — the passes are
    what make the taken ones interpretable, exactly as `paper_coverage`
    had to exist before a signal count could be read.

    Below MIN_EXECUTIONS_FOR_AGGREGATE this returns the rows and NO
    summary statistic. Not a zero: absent. Three fills tell you the fee
    model is not OBVIOUSLY broken, which is not the same as telling you
    it is right.
    """
    if not plane_ready():
        return {}
    s = get_session()
    try:
        q = s.query(PersonalBet)
        if fixture_id is not None:
            q = q.filter_by(fixture_id=fixture_id)
        bets = q.all()
        counts = {k: 0 for k in STATUSES}
        for b in bets:
            counts[b.status] = counts.get(b.status, 0) + 1
        countable = [b for b in bets
                     if b.price_basis == "observed_quote"
                     and b.status != "void"]
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
            "countable": len(countable),
            "excluded_stated_only": sum(
                1 for b in bets if b.price_basis == "stated_only"),
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
        if len(settled) < MIN_EXECUTIONS_FOR_AGGREGATE:
            out["aggregate_withheld"] = {
                "reason": "below_minimum_observations",
                "settled_executions": len(settled),
                "minimum": MIN_EXECUTIONS_FOR_AGGREGATE,
                "note": ("rows are listed; no mean, rate or ROI is "
                         "reported. A summary over this many "
                         "observations would be a narrative."),
            }
        return out
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
              claims: dict | None = None) -> dict:
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
    body = f"🗣 {message}"
    if channel == "action":
        # server-side, unconditionally: the action channel reaches the
        # person whose friend bets real money, so the number's
        # uncertainty travels in the same message, not in a footnote
        # somewhere the reader has to know to look
        body = f"{body}\n{_shadow_qualifier()}"
    s = get_session()
    try:
        row = BroadcastLog(
            fixture_id=fixture_id, channel=channel, source=source,
            session_label=session_label, message=message,
            claims_json=(json.dumps(claims, sort_keys=True, default=str)
                         if claims else None),
            created_at=_now())
        s.add(row)
        s.flush()
        transports: dict = {}
        try:
            from src.alerts import send_alert
            result = send_alert(body, title="Trivela", kind=channel)
            if isinstance(result, dict):
                transports = result
        except Exception as exc:      # a failed send must not lose the record
            print(f"[journal] broadcast dispatch failed: {exc}")
        accepted = sorted(k for k, v in transports.items() if v)
        # true only when at least one transport ACCEPTED the message —
        # "dispatched without raising" is not delivery (journal-P1 F6)
        dispatched = bool(accepted)
        row.delivered = dispatched
        s.commit()
        return {"id": row.id, "dispatched": dispatched,
                "transports": transports, "accepted": accepted,
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
                 "created_at": (r.created_at.isoformat()
                                if r.created_at else None)}
                for r in rows]
    finally:
        s.close()


def open_entries(s, fixture_id: int) -> list[dict]:
    """Journal entries on this fixture that still matter to a live
    reader: everything except void, with their executions."""
    rows = (s.query(PersonalBet).filter_by(fixture_id=fixture_id)
            .filter(PersonalBet.status != "void")
            .order_by(PersonalBet.id.desc()).all())
    out = []
    for b in rows:
        d = _bet_dict(b)
        d["executions"] = [
            _execution_dict(s, e) for e in
            s.query(PersonalBetExecution)
            .filter_by(personal_bet_id=b.id).all()]
        out.append(d)
    return out


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
            out["market_current"] = {
                "basis": "live_read",
                "books": mls.find_all_books(
                    summary.get("date"),
                    (summary.get("home") or {}).get("name") or "",
                    (summary.get("away") or {}).get("name") or ""),
            }
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

        out["journal"] = open_entries(s, fx.id)
        out["said_already"] = recent_broadcasts(fx.id)
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
