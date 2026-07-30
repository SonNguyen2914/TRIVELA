"""The position tracker — the deferred half of the buy/sell-alert request.

Son records his REAL Kalshi positions here; every live cycle prices each
one's hold-to-settlement EV against its cash-out-now value (both fee-aware)
and pushes an EXIT/HOLD read the moment the comparison flips. Alerts ride
the same fan-out as the signals (Discord + ntfy), so nothing depends on a
page being open.

Honesty contract (revised Jul 21): cash-out value is computed at the
polled YES BID minus the taker fee — the price an exit actually realizes.
When no bid is quoted the position is NOT EXECUTABLE: the verdict is
NO_BID (hold-side EV still shown) and no EXIT can fire. The ask is never
silently substituted. The HOLD side has no bias: settlement pays $1 per
contract, entry costs are sunk.

Second half of the same contract (Jul 28, MLS repoint): the HOLD side is
only computable from a probability that is contemporaneous with the match
state being priced. The WC26 path always had one — `live_auto` re-prices
the remainder of a match in progress every cycle. The MLS live plane has
no in-play model at all, so its freshest probability is still the
pre-match one. Pricing an in-play hold against it is wrong in BOTH
directions: for a position that is losing it inflates hold EV and
suppresses the EXIT that should fire, and for one that is winning it
deflates hold EV and manufactures an EXIT out of nothing. So callers
declare `in_play`, rows declare `model_basis`, and a stale pairing yields
the verdict STALE_MODEL — no hold EV, no alert, cash-out value still
shown because that side is pure market data and remains true.
"""
from __future__ import annotations

import time

from sqlalchemy import select

import config
from src.alerts import MODEL_SIGNAL, send_alert
from src.db import SessionLocal, TrackedPosition, utcnow


from src.execution import fee  # noqa: E402  (single economics source)


# verdict cooldown/flip state, in-memory (restart cost: one repeat alert)
_state: dict[int, dict] = {}


def _verdict(p_model: float, bid: float | None, contracts: int,
             cost: float, model_stale: bool = False):
    """(verdict, hold_ev, cashout) — EXIT when selling now beats holding to
    settlement by the margin; HOLD when holding beats selling; else CLOSE.
    `bid` is the SELL side; None means the exit is not executable.

    `model_stale` says the probability is not contemporaneous with the
    state being priced (see the module docstring). Then no hold EV exists
    to compare against, so the verdict is STALE_MODEL — but the cash-out
    figure is still computed and returned, because it comes from the book
    and is true regardless of the model."""
    cashout = (contracts * (bid - fee(bid))) if bid is not None else None
    if model_stale:
        return "STALE_MODEL", None, cashout
    hold_ev = contracts * p_model
    if bid is None:
        return "NO_BID", hold_ev, None
    margin = config.POSITION_FLIP_MARGIN * max(cost, 1.0)
    if cashout - hold_ev >= margin:
        return "EXIT", hold_ev, cashout
    if hold_ev - cashout >= margin:
        return "HOLD", hold_ev, cashout
    return "CLOSE_CALL", hold_ev, cashout


def evaluate_positions(rows_by_market: dict, match_id: str,
                       minute=None, alert: bool = False,
                       alert_title: str = "WC26",
                       alert_label: str | None = None,
                       in_play: bool = False) -> list[dict]:
    """One pass over open tracked positions for a match. `rows_by_market`
    are live_auto (or pre-match batch) rows keyed by market_id, carrying
    live_model_probability/market_probability (live keys preferred, batch
    keys as fallback). Persists nothing; fires EXIT alerts on flips.

    `alert_title` is the ntfy/Discord title; it defaults to the archive
    plane's own label, and the MLS caller passes its own so an MLS alert
    never arrives labelled WC26. `alert_label` names the fixture in the
    message body (MLS market titles are bare side labels like "Austin FC",
    which are ambiguous on their own). `in_play` says the match is in
    progress, which is what makes a `model_basis` of anything other than
    "live" stale — see the module docstring."""
    out = []
    with SessionLocal() as s:
        open_pos = s.execute(
            select(TrackedPosition)
            .where(TrackedPosition.match_id == match_id,
                   TrackedPosition.closed_at.is_(None))
        ).scalars().all()
        for pos in open_pos:
            r = rows_by_market.get(pos.market_id)
            if r is None:
                continue
            p = r.get("live_model_probability")
            if p is None:
                p = r.get("model_probability")
            if p is None:
                continue
            bid = r.get("market_yes_bid")
            # "live" is the default basis so every existing caller — which
            # feeds a contemporaneous re-simulation — is unaffected. Only a
            # row that explicitly declares a non-live basis can go stale,
            # and only while the match is actually in progress.
            basis = r.get("model_basis", "live")
            stale = bool(in_play and basis != "live")
            verdict, hold_ev, cashout = _verdict(p, bid, pos.contracts,
                                                 pos.cost, model_stale=stale)
            item = {"id": pos.id, "market_id": pos.market_id,
                    "market_title": pos.market_title,
                    "match_id": pos.match_id,
                    "entry_price": pos.entry_price,
                    "contracts": pos.contracts, "cost": pos.cost,
                    "live_probability": round(p, 4), "bid": bid,
                    "model_basis": basis, "model_stale": stale,
                    "hold_ev": (round(hold_ev, 2)
                                if hold_ev is not None else None),
                    "cashout_now": (round(cashout, 2)
                                    if cashout is not None else None),
                    "verdict": verdict,
                    "net_if_hold_wins": round(pos.contracts - pos.cost, 2),
                    "net_if_cashout": (round(cashout - pos.cost, 2)
                                       if cashout is not None else None)}
            out.append(item)
            if alert:
                _maybe_alert(pos, item, minute, title=alert_title,
                             label=alert_label)
    return out


def _maybe_alert(pos, item: dict, minute, title: str = "WC26",
                 label: str | None = None) -> None:
    """OFFER a flip read to the alert gate — into exit territory, or back
    to strong hold — with the signals cooldown so a wobbling book can't
    spam. `title`/`label` name the plane the position sits on, so an MLS
    read is never mistaken for a WC26 one.

    A cash-out read is MODEL_SIGNAL: it tells a human to sell a real
    position at a real price, which is precisely the content the money
    lock governs. With REAL_MONEY_SIGNALS_ENABLED false the gate refuses
    it and no transport is touched. `evaluate_positions` still COMPUTES
    the verdict — the read is available on the API and inside the
    narrator's detail brief; what stops is the push.

    A STALE_MODEL read never alerts: both messages below quote a hold EV
    that does not exist in that state, and an alert whose central number
    is unavailable is worse than silence. The read still appears in the
    returned rows, so the state is observable rather than invisible."""
    now = time.time()
    prev = _state.get(pos.id)
    verdict = item["verdict"]
    if prev and prev["verdict"] == verdict:
        return
    if prev and now - prev["ts"] < config.LIVE_SIGNAL_COOLDOWN_SECONDS:
        return
    _state[pos.id] = {"verdict": verdict, "ts": now}
    if verdict == "STALE_MODEL":
        return                       # no hold EV exists to compare against
    if prev is None and verdict != "EXIT":
        return                       # first sighting, nothing urgent
    at = f" @ {minute:.0f}'" if isinstance(minute, (int, float)) else ""
    where = f"{label} — " if label else ""
    if verdict == "EXIT":
        send_alert(
            f"💼 CASH-OUT read{at}: {where}{pos.market_title}\n"
            f"Selling now ≈ ${item['cashout_now']:.0f} beats holding "
            f"(EV ${item['hold_ev']:.0f}; live {item['live_probability']:.0%} "
            f"vs {item['bid']:.2f} bid). Net if you cash: "
            f"{item['net_if_cashout']:+.0f}", title=title,
            dispatch_class=MODEL_SIGNAL)
    elif verdict == "HOLD" and prev and prev["verdict"] == "EXIT":
        send_alert(
            f"💼 back to HOLD{at}: {where}{pos.market_title} — live "
            f"{item['live_probability']:.0%} vs {item['bid']:.2f} bid; "
            f"holding beats cashing again.", title=title,
            dispatch_class=MODEL_SIGNAL)
