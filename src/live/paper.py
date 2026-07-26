"""Execution-quality paper trading (V8.1 evaluation Phase 7).

PAPER ONLY. This simulates what executing the model's positive-edge
signals WOULD have cost, against the FROZEN lock book — it never places
a real order and has zero coupling to the real-money flag. Its purpose
is the execution-strategy evidence the evaluation asks for, kept
strictly separate from forecast quality.

Realism, per the review:
  - entry gates (execution-ready snapshot, quote age, executable ask,
    minimum size, spread, net edge, approved model) — a rejected signal
    keeps its reason, so the ledger has no survivorship bias;
  - fills walk REAL depth (Kalshi's book: buying YES consumes the NO
    bid ladder, yes_ask = 100 - no_bid), consuming size level by level,
    with partial fills when depth runs out — never unlimited size at
    the top;
  - net-of-fee, net-of-slippage economics;
  - a fully-referenced ledger (signal, run, contract, frozen quote,
    requested/filled/price/fee/slippage/latency/reason).

Deterministic: given the frozen quote+depth stream and a fixed policy,
the ledger reproduces exactly (the replay acceptance test). Scoped to
the unambiguous 3-way winner market for v1; prop settlement is a later
extension.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal

import config
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, MarketContract, MarketDepthLevel,
                             MarketQuote, MarketSnapshot, PaperFill,
                             PaperSignal, PredictionContract, PredictionRun)
from src.live.runs import approved_model_version

THREE_WAY = ("home_win", "draw", "away_win")

# Kalshi's trading fee as a VERSIONED, EXACT policy (V9.1 eval F3). The
# general taker formula is ceil_to_centicent(0.07 * C * P * (1-P)) DOLLARS,
# computed once on the whole order. It is evaluated in Decimal, not binary
# float — the float ceil overcharged by 1c at some prices (e.g. 100@$0.10
# gave 64c where the exact value is 63.00c). Series/event overrides and
# maker fees are declared fields, NOT yet populated, so anything that would
# use them stays explicitly approximate and general-taker-only.
FEE_RATE = Decimal("0.07")
CENTICENT = Decimal("0.0001")            # $ precision of Kalshi trade fees
FEE_POLICY = {
    "version": "kalshi-fee-2026-07-general",
    "rate": "0.07",
    "rounding": "ceil_centicent",
    "taker": True,
    "maker_modeled": False,
    "series_overrides": {},              # none populated
    "exit_fees_modeled": False,
    "not_modeled": "series/event overrides, maker fees, exit fees, "
                   "per-order rebate accumulator — general taker only",
}

# The execution policy is versioned so paper results can be tied to the
# exact rules that produced them.
EXEC_POLICY = {
    # v4 (V9.3 eval F1-F4): exact-Decimal depth ordering, per-allocation
    # fees, post-fill edge re-check, and depth-vs-estimate classification.
    "version": "paper-exec-v4",
    "fee_policy": FEE_POLICY["version"],
    "depth_policy": "best_10_each_side",  # V9.1 eval F1
    "min_top_size": 10,       # contracts available at the ask to bother
    "max_spread_c": 8,        # widest yes ask-bid we'll cross
    "max_quote_age_s": 600,   # snapshot freshness ceiling
    "min_net_edge": 0.03,     # model_p - (ask + fee) must clear this
    "target_contracts": 100,  # requested size (depth caps the fill)
    "latency_ms": 250,        # recorded assumption (no movement vs frozen)
    # a fill with NO captured depth is a top-of-book estimate. False keeps
    # it as labelled evidence (excluded from execution-grade metrics);
    # True refuses it outright.
    "require_depth_for_fill": False,
}


def _now():
    return datetime.now(timezone.utc)


def order_fee_dollars(price, contracts) -> Decimal:
    """Kalshi general taker fee for a WHOLE order, EXACT to the centicent
    (V9.1 eval F3): ceil_centicent(0.07 * C * P * (1-P)) in DOLLARS.
    Decimal throughout — the prior float ceil overcharged by 1c at some
    prices. `price` is a probability in [0,1] (dollars per contract)."""
    price = Decimal(str(price))
    contracts = Decimal(str(contracts))
    if contracts <= 0 or price <= 0 or price >= 1:
        return Decimal("0")
    raw = FEE_RATE * contracts * price * (Decimal(1) - price)
    return raw.quantize(CENTICENT, rounding=ROUND_UP)


def _to_cents(dollars) -> int | None:
    """Display helper: exact dollars → integer cents (half-up), for the
    legacy *_c columns. The exact value is retained in the *_dollars
    columns; cents are display only."""
    if dollars is None:
        return None
    return int((Decimal(str(dollars)) * 100).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def _lvl_price_dollars(d) -> Decimal | None:
    """Exact price of a depth level, preferring the provider dollar string
    (V9.1 eval F2), falling back to derived cents."""
    pd = getattr(d, "price_dollars", None)
    if pd:
        try:
            return Decimal(pd)
        except (ArithmeticError, TypeError, ValueError):
            pass
    return Decimal(d.price_c) / 100 if d.price_c is not None else None


def _lvl_size(d) -> Decimal:
    """Exact size of a depth level, preferring the provider *_fp string."""
    sf = getattr(d, "size_fp", None)
    if sf:
        try:
            return Decimal(sf)
        except (ArithmeticError, TypeError, ValueError):
            pass
    return Decimal(d.size or 0)


def yes_buy_ladder(quote: MarketQuote,
                   depth: list) -> list[tuple[Decimal, Decimal]]:
    """The executable BUY-YES ladder as (yes_ask_dollars, size), best
    (lowest ask) first — in EXACT Decimal dollars/sizes (V9.1 eval F2),
    not rounded cents. Kalshi: a resting NO bid at price q IS a YES ask at
    1-q, so we walk the NO depth (now the BEST levels after the F1 fix).
    When no depth was captured, fall back to the top quote's exact ask."""
    levels: list[tuple[Decimal, Decimal]] = []
    for d in depth:
        if d.side != "no":
            continue
        no_bid = _lvl_price_dollars(d)
        size = _lvl_size(d)
        if no_bid is not None and Decimal(0) < no_bid < Decimal(1) \
                and size > 0:
            levels.append((Decimal(1) - no_bid, size))
    if levels:
        levels.sort(key=lambda x: x[0])       # lowest ask first
        return levels
    ask = None
    if getattr(quote, "yes_ask_dollars", None):
        try:
            ask = Decimal(quote.yes_ask_dollars)
        except (ArithmeticError, TypeError, ValueError):
            ask = None
    if ask is None and quote.yes_ask_c is not None:
        ask = Decimal(quote.yes_ask_c) / 100
    if ask is not None:
        size = (_lvl_size_from_fp(getattr(quote, "sizes_fp_json", None),
                                  "yes_ask_size")
                or Decimal(quote.yes_ask_size
                           or EXEC_POLICY["min_top_size"]))
        return [(ask, size)]
    return []


def _lvl_size_from_fp(sizes_fp_json, field) -> Decimal | None:
    if not sizes_fp_json:
        return None
    try:
        import json as _json
        v = _json.loads(sizes_fp_json).get(field)
        return Decimal(str(v)) if v is not None else None
    except (ArithmeticError, TypeError, ValueError):
        return None


def simulate_fill(ladder: list[tuple[Decimal, Decimal]],
                  requested) -> dict:
    """Deterministic depth-walk in EXACT Decimal (V9.1 eval F2). Consumes
    size level by level up the book; partial fill when depth is exhausted.
    Returns exact filled qty, weighted-average price, slippage vs best,
    and levels consumed."""
    requested = Decimal(str(requested))
    filled = Decimal(0)
    cost = Decimal(0)
    used = 0
    best = ladder[0][0] if ladder else None
    # EVERY per-level allocation is retained (V9.3 eval F2). The general
    # fee is non-linear in price (P x (1-P)), so a single fee at the
    # weighted-average price is NOT the sum of the per-fill fees — for
    # 50@0.10 + 50@0.90 that error is $1.75 vs $0.63. The fee engine needs
    # the actual allocations, so they are returned and persisted.
    allocations: list[dict] = []
    for price, size in ladder:
        if filled >= requested:
            break
        take = min(size, requested - filled)
        if take <= 0:
            continue
        filled += take
        cost += take * price
        used += 1
        allocations.append({"seq": used, "price": price, "qty": take})
    if filled == 0:
        return {"filled": Decimal(0), "avg_price": None, "best_ask": best,
                "slippage": None, "levels": 0, "allocations": []}
    avg = cost / filled
    return {"filled": filled, "avg_price": avg, "best_ask": best,
            "slippage": (avg - best) if best is not None else None,
            "levels": used, "notional": cost, "allocations": allocations}


def allocation_fees(allocations: list[dict]) -> tuple[Decimal, list[dict]]:
    """Total fee charged ACROSS the actual fills, plus the per-allocation
    breakdown (V9.3 eval F2).

    Each allocation is charged at ITS OWN price under the general taker
    formula and rounded up to the centicent, then summed — never one fee
    at the blended average, which the non-linear P x (1-P) term makes
    wrong. Still an approximation of the venue's full schedule: maker
    treatment, series overrides, rebates and the balance-rounding fee are
    NOT modelled (see FEE_POLICY.not_modeled)."""
    total = Decimal("0")
    out: list[dict] = []
    for a in allocations:
        fee = order_fee_dollars(a["price"], a["qty"])
        total += fee
        out.append({"seq": a["seq"], "price": str(a["price"]),
                    "qty": str(a["qty"]), "fee": str(fee)})
    return total, out


def _market_gate(quote, snap, net_edge, model_approved) -> str | None:
    """Delegate to the central risk engine — one policy authority for
    every order path (V8.1 eval Phase 8)."""
    from src.live import risk
    pol = EXEC_POLICY
    return risk.market_gate(
        quote, snap, net_edge, min_net_edge=pol["min_net_edge"],
        min_size=pol["min_top_size"], max_spread_c=pol["max_spread_c"],
        max_quote_age_s=pol["max_quote_age_s"],
        model_approved=model_approved)


def paper_trade_lock(run_id: str, backfill: bool = False) -> dict:
    """Generate paper signals + fills for one canonical lock. Idempotent
    (unique per run+contract). PAPER — no real order is ever placed.

    `backfill=True` stamps every signal written with `backfilled_at`, for
    locks recovered after the fact (see backfill_uncovered_locks). The
    ARITHMETIC is identical either way — all of it reads frozen values —
    but the provenance must stay visible in the ledger."""
    if not (plane_ready() and config.MLS_SHADOW_ENABLED
            and config.PAPER_TRADING_ENABLED):
        return {"skipped": "off"}
    s = get_session()
    signals = fills = 0
    try:
        run = s.get(PredictionRun, run_id)
        if run is None or not (run.run_type == "t10" and run.canonical):
            return {"skipped": "not a canonical lock"}
        model_approved = approved_model_version(s) is not None
        snap = (s.get(MarketSnapshot, run.market_snapshot_id)
                if run.market_snapshot_id else None)
        fx = s.get(Fixture, run.fixture_id)
        for c in (s.query(PredictionContract)
                  .filter_by(prediction_run_id=run_id).all()):
            if c.outcome_key not in THREE_WAY or not c.market_quote_id \
                    or not c.market_contract_id:
                continue
            if s.query(PaperSignal).filter_by(
                    prediction_run_id=run_id,
                    market_contract_id=c.market_contract_id).first():
                continue
            quote = s.get(MarketQuote, c.market_quote_id)
            if quote is None:
                continue
            # EXACT ask (V9.1 eval F2): the provider dollar string first,
            # cents only as a fallback — never the rounded cent as the
            # economic input
            ask_d = None
            if quote.yes_ask_dollars:
                try:
                    ask_d = Decimal(quote.yes_ask_dollars)
                except (ArithmeticError, TypeError, ValueError):
                    ask_d = None
            if ask_d is None:
                ask_d = (Decimal(quote.yes_ask_c) / 100
                         if quote.yes_ask_c is not None else Decimal(0))
            # per-unit fee (exact rate, unquantized) for the net-edge gate
            unit_fee = FEE_RATE * ask_d * (Decimal(1) - ask_d)
            net_edge = float(Decimal(str(c.raw_probability))
                             - (ask_d + unit_fee))
            reason = _market_gate(quote, snap, net_edge, model_approved)
            target = EXEC_POLICY["target_contracts"]
            sig = PaperSignal(
                prediction_run_id=run_id,
                market_contract_id=c.market_contract_id,
                market_quote_id=c.market_quote_id,
                fixture_id=run.fixture_id, outcome_key=c.outcome_key,
                policy_version=EXEC_POLICY["version"],
                model_probability=c.raw_probability,
                ask_c=quote.yes_ask_c, ask_dollars=str(ask_d),
                # illustrative whole-order fee at the policy target size
                fee_c=_to_cents(order_fee_dollars(ask_d, target)),
                fee_dollars=str(order_fee_dollars(ask_d, target)),
                net_edge=net_edge,
                decision="reject" if reason else "fill",
                reject_reason=reason, created_at=_now(),
                backfilled_at=(_now() if backfill else None))
            s.add(sig)
            s.flush()
            signals += 1
            if reason:
                continue
            depth = s.query(MarketDepthLevel).filter_by(
                market_quote_id=c.market_quote_id).all()
            # V9.3 eval F4: a ladder built from the top quote because NO
            # order-book depth was captured is a top-of-book ESTIMATE, not
            # a depth-backed execution. Classify it explicitly so
            # execution-grade metrics can exclude it (or reject outright
            # when the policy demands depth).
            exec_class = ("bounded_depth" if depth
                          else "top_of_book_estimate")
            if not depth and EXEC_POLICY["require_depth_for_fill"]:
                sig.decision = "reject"
                sig.reject_reason = "DEPTH_UNAVAILABLE"
                continue
            ladder = yes_buy_ladder(quote, depth)
            fill = simulate_fill(ladder, target)
            if fill["filled"] == 0:
                sig.decision = "reject"
                sig.reject_reason = "DEPTH_INSUFFICIENT"
                continue
            # EXACT economics: fees charged on the ACTUAL per-level
            # allocations (V9.3 eval F2), never one fee at the blended
            # average — the general fee is non-linear in price.
            avg = fill["avg_price"]
            filled = fill["filled"]
            fee_total, alloc_breakdown = allocation_fees(fill["allocations"])
            cost = fill["notional"] + fee_total
            # V9.3 eval F3: the quoted edge authorised this order, but the
            # depth walk may have paid a worse average. Re-apply the policy
            # to the economics the fill ACTUALLY achieved, and refuse the
            # fill if it no longer clears the threshold.
            per_contract_fee = fee_total / filled if filled else Decimal(0)
            realized_edge = float(Decimal(str(c.raw_probability))
                                  - (avg + per_contract_fee))
            sig.realized_net_edge = realized_edge
            if realized_edge < EXEC_POLICY["min_net_edge"]:
                sig.decision = "reject"
                sig.reject_reason = "POST_FILL_EDGE_BELOW_THRESHOLD"
                continue
            cost_c = _to_cents(cost)
            slip = fill["slippage"]
            # EXPOSURE gates — the central risk authority, after the fill
            # cost is known (position size / correlation / bankroll / kill)
            from src.live import risk
            risk_reason = risk.exposure_gate(
                s, fx, c.outcome_key, cost_c, _to_cents(slip))
            if risk_reason:
                sig.decision = "reject"
                sig.reject_reason = risk_reason
                continue
            s.add(PaperFill(
                paper_signal_id=sig.id,
                requested_contracts=target,
                filled_contracts=int(filled), filled_contracts_fp=str(filled),
                avg_fill_price_c=_to_cents(avg),
                avg_fill_price_dollars=str(avg),
                best_ask_c=_to_cents(fill["best_ask"]),
                slippage_c=_to_cents(slip),
                fee_c=_to_cents(fee_total), fee_dollars=str(fee_total),
                cost_c=cost_c, cost_dollars=str(cost),
                levels_consumed=fill["levels"],
                execution_class=exec_class,
                allocations_json=json.dumps(alloc_breakdown),
                fee_policy_version=FEE_POLICY["version"],
                latency_ms=EXEC_POLICY["latency_ms"],
                reason=("partial" if filled < Decimal(str(target))
                        else "filled"),
                created_at=_now(), status="open"))
            fills += 1
        s.commit()
        return {"signals": signals, "fills": fills}
    except Exception as exc:
        s.rollback()
        print(f"[paper] trade failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def settle_paper(fixture_id: int | None = None) -> dict:
    """Settle open paper fills for completed fixtures: a YES contract
    pays 100c per filled contract if its outcome hit, else 0. Determin-
    istic and idempotent (only touches status='open')."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    settled = 0
    try:
        q = (s.query(PaperFill, PaperSignal, Fixture)
             .join(PaperSignal, PaperFill.paper_signal_id == PaperSignal.id)
             .join(Fixture, PaperSignal.fixture_id == Fixture.id)
             .filter(PaperFill.status == "open",
                     Fixture.status == "post",
                     Fixture.home_goals.isnot(None)))
        for fill, sig, fx in q.all():
            if fixture_id is not None and fx.id != fixture_id:
                continue
            result = ("home_win" if fx.home_goals > fx.away_goals else
                      "away_win" if fx.away_goals > fx.home_goals
                      else "draw")
            hit = sig.outcome_key == result
            # EXACT settlement (V9.1 eval F2): a YES contract pays $1.00
            # per EXACT filled contract if the outcome hit, else $0
            filled = Decimal(fill.filled_contracts_fp
                             or fill.filled_contracts or 0)
            cost_d = Decimal(fill.cost_dollars) if fill.cost_dollars \
                else (Decimal(fill.cost_c or 0) / 100)
            payout_d = filled if hit else Decimal(0)
            fill.outcome_hit = hit
            fill.payout_dollars = str(payout_d)
            fill.pnl_dollars = str(payout_d - cost_d)
            fill.payout_c = _to_cents(payout_d)
            fill.pnl_c = _to_cents(payout_d - cost_d)
            fill.status = "settled"
            fill.settled_at = _now()
            settled += 1
        s.commit()
        return {"settled": settled}
    except Exception as exc:
        s.rollback()
        print(f"[paper] settle failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def paper_coverage() -> dict:
    """Did the paper engine actually evaluate every leg it could have?

    The first prospective slate (2026-07-25) exposed the gap this answers.
    15 canonical locks each carried three quote-linked game legs — 45
    eligible legs — and the ledger held 27 signals. Six locks had never
    been paper-traded at all, because `paper_trade_lock` is called inline
    after each lock inside a try/except that must NOT undo the lock when
    paper fails (runs.py). That isolation is right; the silence was not.

    `paper_summary` reported 27 signals with no denominator, so a 40%
    shortfall was indistinguishable from a complete examination that
    happened to find nothing. This is the same defect as the DiskFull
    `{"created": 0}` — a numerator that looks like success on its own.

    Eligibility here is the SAME condition the signal loop tests, so
    `covered` can never be satisfied by a leg the engine would skip."""
    if not plane_ready():
        return {}
    s = get_session()
    try:
        locks = (s.query(PredictionRun)
                 .filter_by(run_type="t10", canonical=True,
                            status="complete").all())
        eligible_legs = signalled = backfilled = 0
        uncovered: list[dict] = []
        partial: list[dict] = []
        for run in locks:
            legs = [c for c in (s.query(PredictionContract)
                                .filter_by(prediction_run_id=run.id).all())
                    if c.outcome_key in THREE_WAY
                    and c.market_contract_id is not None
                    and c.market_quote_id is not None]
            sigs = (s.query(PaperSignal)
                    .filter_by(prediction_run_id=run.id).all())
            eligible_legs += len(legs)
            signalled += len(sigs)
            backfilled += sum(1 for g in sigs if g.backfilled_at is not None)
            if not legs:
                continue
            fx = s.get(Fixture, run.fixture_id) if run.fixture_id else None
            row = {"espn_event_id": (str(fx.espn_event_id) if fx else None),
                   "run_id": run.id, "eligible_legs": len(legs),
                   "signals": len(sigs)}
            if not sigs:
                uncovered.append(row)
            elif len(sigs) < len(legs):
                partial.append(row)
        return {
            "locks_eligible": len(locks),
            "locks_covered": len(locks) - len(uncovered),
            "locks_uncovered": len(uncovered),
            "legs_eligible": eligible_legs,
            "legs_signalled": signalled,
            "legs_missing": max(0, eligible_legs - signalled),
            "coverage_pct": (round(100 * signalled / eligible_legs, 1)
                             if eligible_legs else None),
            "complete": signalled >= eligible_legs and not uncovered,
            "backfilled_signals": backfilled,
            "uncovered": uncovered[:50],
            "partial": partial[:50],
            "note": ("a lock with zero signals means the paper engine never "
                     "ran for it — the lock is still valid; the LEDGER is "
                     "incomplete. Recover with backfill_uncovered_locks()."),
        }
    except Exception as exc:
        print(f"[paper] coverage failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def backfill_uncovered_locks(limit: int = 50) -> dict:
    """Recompute paper signals for canonical locks the engine never ran.

    Faithful by construction: every input the decision reads is FROZEN on
    the lock — the model probability on the prediction contract, the ask
    on the linked quote, and (the one that matters) the quote age the
    staleness gate tests, which is `snapshot.oldest_quote_age_seconds`
    recorded at capture rather than anything computed against now. So a
    recomputation today yields the decision the lock would have produced.

    The signals are still marked `backfilled_at`: deterministic recovery
    is not the same evidence as a signal that existed at lock time, and
    the ledger must never blur the two."""
    cov = paper_coverage()
    out: list[dict] = []
    for row in (cov.get("uncovered") or [])[:limit]:
        res = paper_trade_lock(row["run_id"], backfill=True)
        out.append({"run_id": row["run_id"],
                    "espn_event_id": row["espn_event_id"], **res})
    after = paper_coverage()
    return {"attempted": len(out), "results": out,
            "coverage_before": {k: cov.get(k) for k in
                                ("locks_uncovered", "legs_signalled",
                                 "legs_eligible", "coverage_pct")},
            "coverage_after": {k: v for k, v in after.items()
                               if k not in ("uncovered", "partial", "note")}}


def paper_summary() -> dict:
    """The paper ledger P&L — settled economics + open exposure. All
    labeled PAPER; never a real position."""
    if not plane_ready():
        return {}
    s = get_session()
    try:
        fills = s.query(PaperFill).all()
        settled = [f for f in fills if f.status == "settled"]
        rejects = s.query(PaperSignal).filter_by(decision="reject").count()
        reasons: dict[str, int] = {}
        for r in (s.query(PaperSignal.reject_reason)
                  .filter_by(decision="reject").all()):
            reasons[r[0]] = reasons.get(r[0], 0) + 1
        # V9.3 eval F4: a fill built from the top quote with no captured
        # depth is an ESTIMATE, not a depth-backed execution. Headline P&L
        # is EXECUTION-GRADE ONLY (bounded_depth); estimates are reported
        # separately so they can never silently inflate or deflate ROI.
        graded = [f for f in settled
                  if f.execution_class == "bounded_depth"]
        estimates = [f for f in settled
                     if f.execution_class != "bounded_depth"]
        # exact P&L in dollars (V9.1 eval F2/F3), summed as Decimal
        pnl_d = sum((Decimal(f.pnl_dollars) for f in graded
                     if f.pnl_dollars), Decimal(0))
        cost_d = sum((Decimal(f.cost_dollars) for f in graded
                      if f.cost_dollars), Decimal(0))
        pnl = sum(f.pnl_c or 0 for f in graded)
        cost = sum(f.cost_c or 0 for f in graded)
        est_pnl_d = sum((Decimal(f.pnl_dollars) for f in estimates
                         if f.pnl_dollars), Decimal(0))
        est_cost_d = sum((Decimal(f.cost_dollars) for f in estimates
                          if f.cost_dollars), Decimal(0))
        return {
            "paper": True, "policy_version": EXEC_POLICY["version"],
            "fee_policy": FEE_POLICY["version"],
            "depth_policy": EXEC_POLICY["depth_policy"],
            "fee_basis": FEE_POLICY["not_modeled"],
            "settled_pnl_dollars": str(pnl_d),
            "settled_cost_dollars": str(cost_d),
            "signals": s.query(PaperSignal).count(),
            # the DENOMINATOR (2026-07-26): a signal count alone cannot
            # distinguish "examined the slate and found nothing" from
            # "never examined 40% of it". Never report one without this.
            "coverage": {k: v for k, v in paper_coverage().items()
                         if k not in ("uncovered", "partial")},
            "fills": len(fills),
            "rejected": rejects, "reject_reasons": reasons,
            "open_fills": sum(1 for f in fills if f.status == "open"),
            "settled_fills": len(graded),
            # excluded from the headline: no captured depth backed them
            "execution_grade": "bounded_depth",
            "estimate_only": {
                "settled_fills": len(estimates),
                "settled_pnl_dollars": str(est_pnl_d),
                "settled_cost_dollars": str(est_cost_d),
                "note": ("top-of-book estimates — no captured depth; "
                         "EXCLUDED from the headline P&L and ROI"),
            },
            "settled_cost_c": cost, "settled_pnl_c": pnl,
            "roi_pct": round(100 * pnl / cost, 2) if cost else None,
            "note": ("paper execution against frozen T-10 books — never a "
                     "real order; approximate general fees (series/event "
                     "overrides + maker/taker not modeled); execution "
                     "evidence, not a track record"),
        }
    finally:
        s.close()
