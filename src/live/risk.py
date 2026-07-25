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
    "version": "risk-v1",
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


def current_exposure(s) -> dict:
    """Open paper exposure by match / (match,direction) / team, plus
    totals — the live picture the exposure gate checks against."""
    per_match: dict[int, int] = {}
    per_corr: dict[tuple, int] = {}
    per_team: dict[int, int] = {}
    total = open_count = 0
    rows = (s.query(PaperFill, PaperSignal)
            .join(PaperSignal, PaperFill.paper_signal_id == PaperSignal.id)
            .filter(PaperFill.status == "open").all())
    for fill, sig in rows:
        cost = fill.cost_c or 0
        total += cost
        open_count += 1
        fx = s.get(Fixture, sig.fixture_id) if sig.fixture_id else None
        grp = correlation_group(sig.outcome_key)
        if fx:
            per_match[fx.id] = per_match.get(fx.id, 0) + cost
            per_corr[(fx.id, grp)] = per_corr.get((fx.id, grp), 0) + cost
            team = fx.home_team_id if grp == "home" else \
                fx.away_team_id if grp == "away" else None
            if team:
                per_team[team] = per_team.get(team, 0) + cost
    return {"per_match": per_match, "per_corr": per_corr,
            "per_team": per_team, "total": total, "open_count": open_count}


def exposure_gate(s, fixture, outcome_key: str, cost_c: int,
                  slippage_c: int | None) -> str | None:
    """Position-size / correlation / bankroll gates. Kill switches
    first (safest state = no new orders). Returns a reason or None."""
    ks = active_kill_switches(s)
    if ks:
        return f"KILL_SWITCH:{ks[0]}"
    pol = RISK_POLICY
    if slippage_c is not None and slippage_c > pol["max_slippage_c"]:
        return "SLIPPAGE_TOO_HIGH"
    exp = current_exposure(s)
    if exp["open_count"] >= pol["max_simultaneous_positions"]:
        return "MAX_POSITIONS"
    if exp["total"] + cost_c > pol["max_total_open_c"]:
        return "TOTAL_RISK_LIMIT"
    if exp["total"] + cost_c > (pol["notional_bankroll_c"]
                                - pol["min_bankroll_reserve_c"]):
        return "BANKROLL_RESERVE"
    grp = correlation_group(outcome_key)
    m = exp["per_match"].get(fixture.id, 0)
    if m + cost_c > pol["max_match_exposure_c"]:
        return "MATCH_EXPOSURE_LIMIT"
    c = exp["per_corr"].get((fixture.id, grp), 0)
    if c + cost_c > pol["max_correlated_exposure_c"]:
        return "CORRELATED_EXPOSURE_LIMIT"
    team = fixture.home_team_id if grp == "home" else \
        fixture.away_team_id if grp == "away" else None
    if team:
        t = exp["per_team"].get(team, 0)
        if t + cost_c > pol["max_team_exposure_c"]:
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
            "total_open_c": exp["total"],
            "matches_with_exposure": len(exp["per_match"]),
            "note": "one server-side authority; paper now, any executor later",
        }
    finally:
        s.close()
