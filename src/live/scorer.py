"""Phase 2 — scoring settled journal rows, in the order that survives
a small sample.

The metric order here is a claim about what converges, not a menu:

  1. CLV, closing-line value. Measurable on EVERY entry including the
     losers, so it converges in dozens of observations rather than
     hundreds. It is the primary number.
  2. Calibration, Brier — and always beside the MARKET's Brier on the
     SAME fixtures. A Brier score with no baseline says nothing: 0.21
     is excellent against a coin flip and poor against a sharp book.
  3. P&L, reported LAST and never without an interval. Over a few dozen
     bets it is almost entirely variance; a prior analysis found a +48%
     day sitting at the 98.2nd percentile of its own distribution.

WHAT THIS REFUSES TO DO

An empty corpus reports as EMPTY. It does not report 0.0 CLV, a null
Brier, or an ROI of zero — none of which are results, and all of which
read like ones. Below MIN_OBSERVATIONS the rows are listed and no
aggregate is computed at all (`journal.MIN_EXECUTIONS_FOR_AGGREGATE`,
the same floor the journal already enforces on itself). A slate is not
a lesson.

THE CLOSING PRICE IS THE T-10 LOCK, AND NOTHING ELSE

Verified before this module was written: the canonical T-10
`PredictionRun` points at PredictionContract rows carrying
`market_quote_id`, and those quotes hold the frozen bid/ask captured
~10 minutes before kickoff. So CLV needs no new capture. If a lock is
absent, or its contract carries no quote, that entry is EXCLUDED with
the reason named — never back-filled from a later read, which would be
a post-kickoff price wearing a closing price's label.

`mls_markets_job` captures the book every 10 minutes, so an entry made
before T-10 can cite a fresh quote and still have a strictly later
closing price. An entry that cites THE LOCK'S OWN QUOTE has no gap to
measure — its CLV is zero by construction, not by observation — and is
excluded under `entry_is_the_close` rather than being averaged in as a
genuine zero.
"""
from __future__ import annotations

from decimal import Decimal

from src.live import journal
from src.live.models import (Fixture, MarketContract, MarketQuote,
                             PersonalBet, PredictionContract, PredictionRun)

MIN_OBSERVATIONS = journal.MIN_EXECUTIONS_FOR_AGGREGATE
THREE_WAY = ("home_win", "draw", "away_win")

# Why mid-to-mid: the entry may be a buy and the close a two-sided
# quote, so ask-to-ask compares different things as the spread moves.
# The mid is the same construction on both sides. Stated here because a
# CLV number is meaningless without its convention.
CLV_CONVENTION = ("mid(close) - mid(entry), in dollars of probability. "
                  "Positive = the market moved TOWARD the entry, i.e. "
                  "the entry beat the close.")


def _mid(q) -> Decimal | None:
    return journal._mid(q)


def _outcome_of(fx: Fixture) -> str | None:
    """The settled 3-way result, or None when the fixture has no score."""
    if fx.home_goals is None or fx.away_goals is None:
        return None
    if fx.home_goals > fx.away_goals:
        return "home_win"
    if fx.away_goals > fx.home_goals:
        return "away_win"
    return "draw"


def _lock_for(s, fixture_id: int):
    return (s.query(PredictionRun)
            .filter_by(fixture_id=fixture_id, run_type="t10",
                       canonical=True, status="complete").first())


def _closing_quotes(s, lock) -> dict:
    """outcome_key -> (quote, model_probability) frozen at the lock."""
    out = {}
    for c in (s.query(PredictionContract)
              .filter_by(prediction_run_id=lock.id).all()):
        if not c.outcome_key:
            continue
        q = s.get(MarketQuote, c.market_quote_id) if c.market_quote_id \
            else None
        out[c.outcome_key] = (q, c.raw_probability)
    return out


def _devigged_close(closing: dict) -> dict | None:
    """The market's own 3-way probabilities at the close.

    Normalising the three mids removes the overround. Returns None
    unless all three legs are priced — two legs normalised into a
    fake three-way is exactly the guess the decision sheet refuses to
    make, and the calibration baseline must not make it either.
    """
    mids = {}
    for k in THREE_WAY:
        q = (closing.get(k) or (None, None))[0]
        m = _mid(q)
        if m is None:
            return None
        mids[k] = m
    total = sum(mids.values())
    if total <= 0:
        return None
    return {k: float(v / total) for k, v in mids.items()}


def _rows(s, competition_slug: str) -> tuple[list, dict]:
    """Countable settled entries, plus the census of what was dropped.

    Every exclusion is counted and named. A scorer that silently
    narrowed its own denominator would be the failure this whole
    platform is built to avoid.
    """
    bets = (s.query(PersonalBet)
            .filter_by(competition_slug=competition_slug).all())
    superseded = journal._superseded_ids(s, [b.id for b in bets])
    excluded: dict = {}

    def drop(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    kept = []
    for b in bets:
        if not journal._counts_toward_aggregate(b, b.id in superseded):
            if b.price_basis != "observed_quote":
                drop("stated_only")
            elif b.status == "void":
                drop("void_recorded_after_kickoff")
            elif b.id in superseded:
                drop("superseded_by_a_correction")
            else:
                drop("quote_not_contemporaneous")
            continue
        fx = s.get(Fixture, b.fixture_id)
        if fx is None:
            drop("fixture_missing")
            continue
        actual = _outcome_of(fx)
        if actual is None:
            drop("fixture_not_settled")
            continue
        lock = _lock_for(s, fx.id)
        if lock is None:
            drop("no_canonical_t10_lock")
            continue
        closing = _closing_quotes(s, lock)
        cq, model_p = closing.get(b.outcome_key or "", (None, None))
        if cq is None:
            drop("lock_carries_no_quote_for_this_outcome")
            continue
        if b.market_quote_id is not None and b.market_quote_id == cq.id:
            drop("entry_is_the_close")
            continue
        kept.append({"bet": b, "fixture": fx, "actual": actual,
                     "lock": lock, "closing": closing,
                     "close_quote": cq, "lock_model_p": model_p})
    return kept, excluded


def _mean_ci(xs: list[float]) -> dict:
    """Mean with a normal-approximation 95% interval.

    Reported WITH n and the interval, never alone. A point estimate
    whose interval includes zero is not an effect, and the field name
    says so rather than leaving it to the reader.
    """
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None,
                "reason": "no observations"}
    mean = sum(xs) / n
    if n < 2:
        return {"n": n, "mean": round(mean, 6), "ci95": None,
                "significant": False,
                "note": "a single observation has no interval"}
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    se = (var / n) ** 0.5
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    return {"n": n, "mean": round(mean, 6),
            "ci95": [round(lo, 6), round(hi, 6)],
            "significant": lo > 0 or hi < 0,
            "note": ("the interval includes zero — this is not an "
                     "established effect" if lo <= 0 <= hi else
                     "the interval excludes zero")}


def score(competition_slug: str = "mls-2026") -> dict:
    """CLV, then calibration against the market, then P&L. In that order."""
    from src.live.db import get_session, plane_ready

    out: dict = {
        "competition_slug": competition_slug,
        "evidence_class": "personal_journal",
        "counts_toward_edge": False,
        "note": ("human-selected entries: this establishes execution "
                 "fidelity and CLV, never model edge. Never sums with "
                 "the paper ledger."),
        "minimum_observations": MIN_OBSERVATIONS,
    }
    if not plane_ready():
        out["corpus"] = "unavailable"
        out["reason"] = "live plane dormant — no journal to score"
        return out
    s = get_session()
    try:
        kept, excluded = _rows(s, competition_slug)
        out["excluded"] = excluded
        out["countable"] = len(kept)

        if not kept:
            out["corpus"] = "empty"
            out["clv"] = out["calibration"] = out["pnl"] = {
                "available": False,
                "reason": ("no countable settled entries" if not excluded
                           else "every entry was excluded; see `excluded`"),
                "means": ("EMPTY, not zero. No CLV, Brier or P&L is "
                          "computed or implied."),
            }
            return out
        out["corpus"] = "populated"

        # ---------------- 1. CLV — the primary metric ------------------
        clv_rows = []
        for r in kept:
            b = r["bet"]
            entry_q = s.get(MarketQuote, b.market_quote_id)
            em, cm = _mid(entry_q), _mid(r["close_quote"])
            if em is None or cm is None:
                continue
            clv_rows.append({
                "bet_id": b.id, "fixture_id": b.fixture_id,
                "outcome_key": b.outcome_key,
                "entry_mid": float(em), "close_mid": float(cm),
                "clv": float(cm - em),
            })
        out["clv"] = {"available": bool(clv_rows),
                      "convention": CLV_CONVENTION,
                      "rows": clv_rows}
        if not clv_rows:
            out["clv"]["reason"] = "no entry/close mid pair was computable"
        elif len(clv_rows) < MIN_OBSERVATIONS:
            out["clv"]["aggregate_withheld"] = {
                "reason": "below_minimum_observations",
                "observations": len(clv_rows),
                "minimum": MIN_OBSERVATIONS,
                "note": ("rows are listed; no mean is reported. A "
                         "summary over this many observations would be "
                         "a narrative."),
            }
        else:
            out["clv"]["aggregate"] = _mean_ci([r["clv"] for r in clv_rows])
            out["clv"]["beat_close_rate"] = round(
                sum(1 for r in clv_rows if r["clv"] > 0) / len(clv_rows), 4)

        # ------- 2. calibration — ALWAYS beside the market's -----------
        ours, theirs, pairs = [], [], []
        no_partition = 0
        for r in kept:
            b = r["bet"]
            if b.outcome_key not in THREE_WAY:
                no_partition += 1
                continue
            devig = _devigged_close(r["closing"])
            if devig is None:
                no_partition += 1
                continue
            p_model = b.model_probability
            if p_model is None:
                no_partition += 1
                continue
            y = 1.0 if r["actual"] == b.outcome_key else 0.0
            bm = (float(p_model) - y) ** 2
            bk = (devig[b.outcome_key] - y) ** 2
            ours.append(bm)
            theirs.append(bk)
            pairs.append({"bet_id": b.id, "outcome_key": b.outcome_key,
                          "hit": bool(y), "model_p": float(p_model),
                          "market_p": round(devig[b.outcome_key], 4),
                          "brier_model": round(bm, 6),
                          "brier_market": round(bk, 6)})
        cal: dict = {"available": bool(pairs), "rows": pairs,
                     "excluded_no_partition": no_partition,
                     "means": ("Brier is reported ONLY beside the market's "
                               "on the SAME entries. A model Brier alone "
                               "has no baseline and states nothing.")}
        if not pairs:
            cal["reason"] = ("no entry had both a frozen model probability "
                             "and a fully-priced three-way close")
        elif len(pairs) < MIN_OBSERVATIONS:
            cal["aggregate_withheld"] = {
                "reason": "below_minimum_observations",
                "observations": len(pairs), "minimum": MIN_OBSERVATIONS}
        else:
            cal["brier_model"] = round(sum(ours) / len(ours), 6)
            cal["brier_market"] = round(sum(theirs) / len(theirs), 6)
            cal["difference"] = _mean_ci(
                [t - o for o, t in zip(ours, theirs)])
            cal["difference"]["means"] = (
                "market Brier minus model Brier. Positive = the model "
                "scored better. Read the interval before the point.")
        out["calibration"] = cal

        # ------------- 3. P&L — last, and never bare -------------------
        out["pnl"] = _pnl(s, kept)
    finally:
        s.close()
    return out


def _fill_cost(ex) -> Decimal | None:
    """What a fill actually cost: price x contracts, plus the fee.

    None when the row records no fill economics — a `not_filled` row is
    liquidity evidence, not a loss, and must never be scored as one.
    """
    if not ex.fill_price_dollars or not ex.filled_contracts:
        return None
    try:
        cost = (Decimal(str(ex.fill_price_dollars))
                * Decimal(str(ex.filled_contracts)))
        if ex.fee_paid_dollars:
            cost += Decimal(str(ex.fee_paid_dollars))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return cost


def _pnl(s, kept: list) -> dict:
    """Realised P&L over entries that were TAKEN and actually filled.

    `considered` and `passed` carry no money and are excluded here while
    still counting in CLV and calibration — which is the point of
    recording them. An honest denominator needs the passes; a P&L does
    not.
    """
    from src.live.models import PersonalBetExecution
    rows = []
    for r in kept:
        b = r["bet"]
        if b.status != "taken":
            continue
        for ex in (s.query(PersonalBetExecution)
                   .filter_by(personal_bet_id=b.id).all()):
            if ex.settlement_credit_dollars is None:
                continue          # unsettled: no return exists yet
            cost = _fill_cost(ex)
            if cost is None:
                continue          # not_filled, or economics not recorded
            pnl = Decimal(str(ex.settlement_credit_dollars)) - cost
            rows.append({"bet_id": b.id, "execution_id": ex.id,
                         "status": ex.status, "cost": str(cost),
                         "credit": str(ex.settlement_credit_dollars),
                         "pnl": float(pnl)})
    if not rows:
        return {"available": False,
                "reason": "no settled executions on taken entries",
                "means": ("EMPTY, not break-even. Nothing was staked, so "
                          "no return exists to report.")}
    out = {"available": True, "rows": rows,
           "means": ("reported LAST and never without an interval. Over a "
                     "few dozen bets P&L is almost entirely variance.")}
    if len(rows) < MIN_OBSERVATIONS:
        out["aggregate_withheld"] = {
            "reason": "below_minimum_observations",
            "observations": len(rows), "minimum": MIN_OBSERVATIONS,
            "note": ("no mean, ROI or total is reported. At this sample "
                     "size a headline number would be a narrative.")}
        return out
    out["total_pnl"] = round(sum(r["pnl"] for r in rows), 4)
    out["total_cost"] = round(sum(float(r["cost"]) for r in rows), 4)
    out["per_bet"] = _mean_ci([r["pnl"] for r in rows])
    return out
