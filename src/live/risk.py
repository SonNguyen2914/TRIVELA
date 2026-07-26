"""Central risk engine (V8.1 evaluation Phase 8).

ONE server-side policy authority, shared by paper trading now and any
future recommender or executor — so no order path can bypass it. It
enforces two gate classes and explains every rejection:

  MARKET gates    the book must be tradeable at all:
                  MODEL_NOT_APPROVED, NOT_EXECUTION_READY, QUOTE_STALE,
                  NO_EXECUTABLE_ASK, INSUFFICIENT_SIZE, SPREAD_TOO_WIDE,
                  NET_EDGE_TOO_LOW, SLIPPAGE_TOO_HIGH, DEPTH_INSUFFICIENT
  EXPOSURE gates  the position must fit the risk budget:
                  MAX_POSITIONS, TOTAL_RISK_LIMIT, MATCH_EXPOSURE_LIMIT,
                  CORRELATED_EXPOSURE_LIMIT, TEAM_EXPOSURE_LIMIT,
                  BANKROLL_RESERVE

Above both sit KILL SWITCHES (config + data-driven). The safest state
is no new orders: any active switch rejects everything. Limits are
explicit versioned policy settings, never hidden constants. Correlated
markets on one match (home win / home −1.5 / home team over / home
first goal all express "home does well") share a match-direction
budget, so the system can't stack the same opinion across families.
"""
from __future__ import annotations

from decimal import Decimal

from datetime import datetime, timezone

import config
from src.live.models import Fixture, PaperFill, PaperSignal

RISK_POLICY = {
    # v2 (V9.5 eval H4): every gate compares EXACT Decimal dollars. v1
    # converted cost and slippage to integer cents first, so an order
    # costing $60.004 cleared a $60.000 correlated limit and 3.004c of
    # slippage cleared a 3c ceiling. The limits stay declared in cents —
    # they are the policy numbers — but the arithmetic is now exact.
    "version": "risk-v2",
    "notional_bankroll_c": 100_000,       # $1,000 paper bankroll
    "min_bankroll_reserve_c": 20_000,     # keep $200 unspent
    "max_contracts_per_order": 100,
    "max_match_exposure_c": 10_000,       # $100 across one match
    "max_correlated_exposure_c": 6_000,   # $60 per (match, direction)
    "max_team_exposure_c": 20_000,        # $200 per team, all matches
    "max_total_open_c": 40_000,           # $400 open at once
    "max_simultaneous_positions": 40,
    "max_slippage_c": 3,                  # cents above best ask
    "max_market_data_age_s": 900,         # data-driven kill-switch trip
}

# outcome_key -> the match DIRECTION it expresses. Correlated families
# collapse to one budget so "home win" + "home -1.5" + "home team over"
# don't each get a full allocation of the same opinion.
_DIRECTION_PREFIX = (
    ("home", "home"), ("away", "away"), ("draw", "draw"),
    ("over_", "over"), ("under_", "under"), ("btts", "over"),
    ("score_", "score"), ("no_goal", "under"),
)


def _now():
    return datetime.now(timezone.utc)


def correlation_group(outcome_key: str) -> str:
    for prefix, group in _DIRECTION_PREFIX:
        if (outcome_key or "").startswith(prefix):
            return group
    return "other"


def active_kill_switches(s) -> list[str]:
    """Config switches plus data-driven ones. Any entry halts new
    orders. DAILY_LOSS_LIMIT trips off the settled paper P&L."""
    active = []
    if config.GLOBAL_TRADING_DISABLED:
        active.append("GLOBAL_TRADING_DISABLED")
    if config.COMPETITION_TRADING_DISABLED:
        active.append("COMPETITION_TRADING_DISABLED")
    # market data staleness — the freshest lock snapshot's quote age
    try:
        from src.live.models import MarketSnapshot
        latest = (s.query(MarketSnapshot)
                  .filter_by(status="complete")
                  .order_by(MarketSnapshot.captured_at.desc()).first())
        if latest and latest.oldest_quote_age_seconds is not None \
                and latest.oldest_quote_age_seconds \
                > RISK_POLICY["max_market_data_age_s"]:
            # informational only for PAPER (fills already require
            # execution_ready); a real executor would hard-halt here
            pass
    except Exception:
        pass
    # daily loss limit off settled paper P&L (a negative day halts new)
    try:
        pnl = sum(f.pnl_c or 0 for f in s.query(PaperFill)
                  .filter_by(status="settled").all()
                  if f.settled_at and _utc(f.settled_at).date()
                  == _now().date())
        if pnl <= -RISK_POLICY["max_match_exposure_c"]:
            active.append("DAILY_LOSS_LIMIT")
    except Exception:
        pass
    return active


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _exact_price(quote, field: str) -> Decimal | None:
    """The exact provider price for a quote field, preferring the
    fixed-point dollar string and falling back to legacy integer cents."""
    ds = getattr(quote, f"{field}_dollars", None)
    if ds:
        try:
            return Decimal(ds)
        except (ArithmeticError, TypeError, ValueError):
            pass
    c = getattr(quote, f"{field}_c", None)
    return (Decimal(c) / 100) if c is not None else None


def _exact_size(quote, field: str) -> Decimal:
    """The exact size, preferring the fractional *_fp string."""
    import json as _json
    blob = getattr(quote, "sizes_fp_json", None)
    if blob:
        try:
            v = (_json.loads(blob) or {}).get(field)
            if v is not None:
                return Decimal(str(v))
        except (ArithmeticError, TypeError, ValueError):
            pass
    return Decimal(getattr(quote, field, 0) or 0)


def market_gate(quote, snapshot, net_edge: float,
                min_net_edge: float, min_size: int,
                max_spread_c: int, max_quote_age_s: int,
                model_approved: bool) -> str | None:
    """Tradeability gates. Returns a rejection reason or None."""
    if not model_approved:
        return "MODEL_NOT_APPROVED"
    if not (snapshot and snapshot.execution_ready):
        return "NOT_EXECUTION_READY"
    if snapshot.oldest_quote_age_seconds is not None \
            and snapshot.oldest_quote_age_seconds > max_quote_age_s:
        return "QUOTE_STALE"
    # EXACT economics, not the rounded display cents (V9.3 eval F13). Near
    # a threshold a subpenny price or fractional size can flip the decision,
    # and the ledger already stores exact values — the gate must agree with
    # them. The *_c / integer fields remain only as a fallback for legacy
    # rows that predate the exact columns.
    ask_d = _exact_price(quote, "yes_ask")
    bid_d = _exact_price(quote, "yes_bid")
    size_d = _exact_size(quote, "yes_ask_size")
    if ask_d is None:
        return "NO_EXECUTABLE_ASK"
    if size_d < Decimal(str(min_size)):
        return "INSUFFICIENT_SIZE"
    if bid_d is not None \
            and (ask_d - bid_d) > (Decimal(str(max_spread_c)) / 100):
        return "SPREAD_TOO_WIDE"
    if net_edge <= min_net_edge:
        return "NET_EDGE_TOO_LOW"
    return None


def limit_dollars(key: str) -> Decimal:
    """A cent-denominated policy limit as EXACT Decimal dollars."""
    return Decimal(RISK_POLICY[key]) / 100


def _fill_cost_dollars(fill) -> Decimal:
    """A fill's cost in exact dollars. `cost_dollars` is the provider-
    precision value; the integer cents are a legacy display fallback."""
    if getattr(fill, "cost_dollars", None):
        try:
            return Decimal(fill.cost_dollars)
        except (ArithmeticError, TypeError, ValueError):
            pass
    return Decimal(fill.cost_c or 0) / 100


def empty_exposure() -> dict:
    return {"per_match": {}, "per_corr": {}, "per_team": {},
            "total": Decimal(0), "open_count": 0}


def add_exposure(exp: dict, fixture, outcome_key: str,
                 cost_d: Decimal) -> dict:
    """Fold one accepted fill into an exposure picture.

    Needed because a lock's legs are evaluated in sequence and each
    accepted fill must count against the next leg's budget — which is
    what happened at lock time, and therefore what a faithful replay
    must reproduce."""
    grp = correlation_group(outcome_key)
    exp["total"] = exp["total"] + cost_d
    exp["open_count"] = exp["open_count"] + 1
    if fixture is not None:
        fid = fixture.id
        exp["per_match"][fid] = exp["per_match"].get(fid, Decimal(0)) + cost_d
        key = (fid, grp)
        exp["per_corr"][key] = exp["per_corr"].get(key, Decimal(0)) + cost_d
        team = fixture.home_team_id if grp == "home" else \
            fixture.away_team_id if grp == "away" else None
        if team:
            exp["per_team"][team] = (exp["per_team"].get(team, Decimal(0))
                                     + cost_d)
    return exp


def current_exposure(s) -> dict:
    """Open paper exposure by match / (match,direction) / team, plus
    totals, in EXACT Decimal dollars (V9.5 eval H4 — it summed rounded
    cents). This is the LIVE picture; a paper decision must read the
    exposure frozen on its evaluation context instead."""
    exp = empty_exposure()
    rows = (s.query(PaperFill, PaperSignal)
            .join(PaperSignal, PaperFill.paper_signal_id == PaperSignal.id)
            .filter(PaperFill.status == "open").all())
    for fill, sig in rows:
        fx = s.get(Fixture, sig.fixture_id) if sig.fixture_id else None
        add_exposure(exp, fx, sig.outcome_key, _fill_cost_dollars(fill))
    return exp


def exposure_gate(fixture, outcome_key: str, cost_d: Decimal,
                  slippage_d: Decimal | None, *,
                  kill_switches: list[str], exposure: dict) -> str | None:
    """Position-size / correlation / bankroll gates. Kill switches first
    (safest state = no new orders). Returns a reason or None.

    PURE (V9.5 eval C1): the two mutable inputs — active kill switches
    and open exposure — are passed in rather than queried. v1 read them
    from the live database, so replaying a frozen lock later could
    produce a different decision; the evaluator demonstrated exactly
    that by tripping a kill switch. Callers supply either the live
    picture or the one frozen on the lock's evaluation context.

    Amounts are EXACT Decimal dollars (V9.5 eval H4)."""
    if kill_switches:
        return f"KILL_SWITCH:{kill_switches[0]}"
    pol = RISK_POLICY
    if slippage_d is not None and slippage_d > limit_dollars("max_slippage_c"):
        return "SLIPPAGE_TOO_HIGH"
    exp = exposure
    if exp["open_count"] >= pol["max_simultaneous_positions"]:
        return "MAX_POSITIONS"
    total = exp["total"] + cost_d
    if total > limit_dollars("max_total_open_c"):
        return "TOTAL_RISK_LIMIT"
    if total > (limit_dollars("notional_bankroll_c")
                - limit_dollars("min_bankroll_reserve_c")):
        return "BANKROLL_RESERVE"
    grp = correlation_group(outcome_key)
    fid = fixture.id if fixture is not None else None
    m = exp["per_match"].get(fid, Decimal(0))
    if m + cost_d > limit_dollars("max_match_exposure_c"):
        return "MATCH_EXPOSURE_LIMIT"
    c = exp["per_corr"].get((fid, grp), Decimal(0))
    if c + cost_d > limit_dollars("max_correlated_exposure_c"):
        return "CORRELATED_EXPOSURE_LIMIT"
    team = None
    if fixture is not None:
        team = fixture.home_team_id if grp == "home" else \
            fixture.away_team_id if grp == "away" else None
    if team:
        t = exp["per_team"].get(team, Decimal(0))
        if t + cost_d > limit_dollars("max_team_exposure_c"):
            return "TEAM_EXPOSURE_LIMIT"
    return None


def assess() -> dict:
    """Operator view: policy, active kill switches, current exposure."""
    from src.live.db import get_session, plane_ready
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    try:
        exp = current_exposure(s)
        return {
            "policy_version": RISK_POLICY["version"],
            "policy": RISK_POLICY,
            "active_kill_switches": active_kill_switches(s),
            "open_positions": exp["open_count"],
            "total_open_dollars": str(exp["total"]),
            "total_open_c": int(exp["total"] * 100),   # display only
            "matches_with_exposure": len(exp["per_match"]),
            "note": "one server-side authority; paper now, any executor later",
        }
    finally:
        s.close()
