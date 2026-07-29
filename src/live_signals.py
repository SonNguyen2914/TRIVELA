"""In-play BUY/SELL signals on WATCHED markets.

"Watch" is Son's declaration: I'm betting (or holding) this market. Once
the match is live, the self-running live read re-prices every open book
from the remaining-match simulation; when that live model probability
diverges from the market's own price beyond LIVE_SIGNAL_MIN_DIFF, this
module fires a signal:

  BUY  — live model prices YES above the market (model − market >= +thr):
         the position looks cheap right now, add/enter.
  SELL — live model prices YES below the market (model − market <= −thr):
         the market is paying more than the model thinks it's worth,
         exit/take profit.

A second scan covers EVERY open book, watched or not: EASY WIN — the live
model calls the outcome near-certain (>= LIVE_EASYWIN_MIN_PROB) while the
price still pays (<= LIVE_EASYWIN_MAX_PRICE) and hasn't fully caught up
(gap >= LIVE_EASYWIN_MIN_DIFF). Watched markets are excluded from that
scan — they already get the sharper BUY/SELL treatment above.

Anti-spam: a market re-fires only after LIVE_SIGNAL_COOLDOWN_SECONDS have
passed AND the read materially changed — the side flipped, or the
divergence strengthened by >= RESTRENGTHEN beyond the last fired value.

These are deliberate, narrow exceptions to the "live edge is informational
only" rule — explicit pings Son asked for, never a board-wide TAKE
resurrection. Model-only rows (Kalshi book closed) can't fire — there is
nothing to buy or sell.
"""
from __future__ import annotations

import time

from sqlalchemy import select

import config
from src.alerts import MODEL_SIGNAL, send_alert
from src.cache import latest_for_match
from src.db import LiveSignal, MatchLiveSnapshot, SessionLocal, WatchlistItem
from src.live_auto import live_auto
from src.positions import evaluate_positions
from src.schedule_data import load_schedule

# A repeat signal on the same side needs the divergence to have grown by at
# least this much past the previously fired value ("it got MORE mispriced").
RESTRENGTHEN = 0.05

# market_id -> {"side": str, "diff": float, "ts": float} — last FIRED signal.
# In-memory on purpose: after a restart the worst case is one repeated
# signal per watched market, which is honest anyway (the read still holds).
_state: dict[str, dict] = {}


def _decide(row: dict) -> tuple[str, float] | None:
    """BUY/SELL side for one priced market row, or None inside the band."""
    model_p = row.get("live_model_probability")
    market_p = row.get("market_probability")
    if model_p is None or market_p is None:      # model-only row: no book
        return None
    diff = model_p - market_p
    # tiny epsilon so an exactly-at-threshold gap fires despite float noise
    # (prices are whole cents: 0.62 - 0.54 must count as 0.08, not 0.0799…)
    thr = config.LIVE_SIGNAL_MIN_DIFF - 1e-9
    if diff >= thr:
        return "BUY", diff
    if diff <= -thr:
        return "SELL", diff
    return None


def _decide_easy(row: dict) -> tuple[str, float] | None:
    """EASY WIN for one priced row: near-certain per the live model, price
    still pays, market not fully caught up. Always BUY-side by nature."""
    model_p = row.get("live_model_probability")
    market_p = row.get("market_probability")
    if model_p is None or market_p is None:
        return None
    diff = model_p - market_p
    eps = 1e-9
    if (model_p >= config.LIVE_EASYWIN_MIN_PROB - eps
            and market_p <= config.LIVE_EASYWIN_MAX_PRICE + eps
            and diff >= config.LIVE_EASYWIN_MIN_DIFF - eps):
        return "BUY", diff
    return None


def _should_fire(market_id: str, side: str, diff: float,
                 now_ts: float | None = None) -> bool:
    prev = _state.get(market_id)
    if prev is None:
        return True
    now_ts = time.time() if now_ts is None else now_ts
    if now_ts - prev["ts"] < config.LIVE_SIGNAL_COOLDOWN_SECONDS:
        return False
    if side != prev["side"]:
        return True
    return abs(diff) - abs(prev["diff"]) >= RESTRENGTHEN


def _mark_fired(market_id: str, side: str, diff: float,
                now_ts: float | None = None) -> None:
    _state[market_id] = {"side": side, "diff": diff,
                         "ts": time.time() if now_ts is None else now_ts}


def _fire(match, row: dict, side: str, diff: float, kind: str,
          minute, fallback_title: str | None = None) -> None:
    """Persist one signal and OFFER it to the alert gate.

    The row is written either way — the signal is evidence about what
    the live model said, and that record is not what the money lock
    governs. The DISPATCH is MODEL_SIGNAL by construction: a BUY/SELL or
    EASY-WIN push is computed betting content aimed at the channel a
    consenting third party acts on. With REAL_MONEY_SIGNALS_ENABLED
    false the gate refuses it, touches no transport, and records the
    refusal against this call site."""
    title = row.get("market_title") or fallback_title or row["market_id"]
    with SessionLocal() as s:
        s.add(LiveSignal(
            match_id=match.match_id, market_id=row["market_id"],
            market_title=title, side=side, kind=kind,
            live_probability=row["live_model_probability"],
            market_probability=row["market_probability"],
            difference=round(diff, 4),
            minute=minute))
        s.commit()
    if kind == "easy_win":
        head = "💰 **EASY WIN**"
    else:
        head = f"{'🟢' if side == 'BUY' else '🔴'} **{side} SIGNAL**"
    min_str = f" ({minute:.0f}')" if minute is not None else ""
    send_alert(
        f"{head} — {match.home} vs {match.away}{min_str}\n"
        f"**{title}**\n"
        f"Live model {row['live_model_probability']:.0%} vs "
        f"market {row['market_probability']:.0%} "
        f"({diff:+.0%})",
        dispatch_class=MODEL_SIGNAL)


def evaluate_live_signals(engine) -> dict:
    """One evaluation pass over every LIVE match, riding the same
    ~25s-cached live_auto cycle the frontend stream reads (so this is
    nearly free). Two scans per match: watched-market BUY/SELL against
    LIVE_SIGNAL_MIN_DIFF, and the EASY-WIN sweep across every other open
    book. New signals persist + push; cooldowns keep them meaningful."""
    checked = fired = 0
    with SessionLocal() as s:
        items = s.execute(select(WatchlistItem)).scalars().all()
        watched_by_match: dict[str, list] = {}
        for w in items:
            watched_by_match.setdefault(w.match_id, []).append(
                {"market_id": w.market_id, "market_title": w.market_title})
        live_ids = set(s.execute(select(MatchLiveSnapshot.match_id)).scalars())

    if not live_ids:
        return {"checked": 0, "fired": 0}

    for match in load_schedule():
        if match.match_id not in live_ids:
            continue
        watched = watched_by_match.get(match.match_id, [])
        try:
            out = live_auto(match, engine,
                            (latest_for_match(match.match_id) or {}).get("xg"))
        except Exception as exc:   # a feed hiccup must never kill the job
            print(f"[live-signals] {match.match_id} cycle failed: {exc}")
            continue
        if not out.get("available"):
            continue
        rows = {r["market_id"]: r for r in out.get("markets", [])}
        minute = (out.get("live_state") or {}).get("minutes_elapsed")
        watched_ids = {w["market_id"] for w in watched}

        # -- scan 1: BUY/SELL on the markets Son is betting ---------------
        for w in watched:
            row = rows.get(w["market_id"])
            if row is None:
                continue
            checked += 1
            verdict = _decide(row)
            if verdict is None:
                continue
            side, diff = verdict
            if not _should_fire(w["market_id"], side, diff):
                continue
            _mark_fired(w["market_id"], side, diff)
            fired += 1
            _fire(match, row, side, diff, "watched", minute,
                  fallback_title=w["market_title"])

        # -- position tracker: hold-vs-cashout reads on Son's REAL bets ---
        pos_reads: list = []
        try:
            pos_reads = evaluate_positions(rows, match.match_id, minute,
                                           alert=True)
        except Exception as exc:
            print(f"[positions] {match.match_id} eval failed: {exc}")

        # -- narrator: the live read, posted to the detail channel --------
        try:
            from src.narrator import narrate
            narrate(match, out, pos_reads)
        except Exception as exc:
            print(f"[narrator] {match.match_id} failed: {exc}")

        # -- scan 2: EASY WIN across every other open book ----------------
        # Kalshi can list one outcome under two families (KXWCGAME 3-way +
        # KXWCMOV moneyline). Collapse candidates per outcome_key to the
        # cheapest book and key the cooldown on match+outcome, so a single
        # outcome can never double-ping across families.
        easy_best: dict[str, tuple[dict, str, float]] = {}
        for mkt_id, row in rows.items():
            if mkt_id in watched_ids:
                continue
            verdict = _decide_easy(row)
            if verdict is None:
                continue
            side, diff = verdict
            okey = row.get("outcome_key") or mkt_id
            best = easy_best.get(okey)
            if best is None or row["market_probability"] < best[0]["market_probability"]:
                easy_best[okey] = (row, side, diff)
        for okey, (row, side, diff) in easy_best.items():
            key = f"easy:{match.match_id}:{okey}"
            if not _should_fire(key, side, diff):
                continue
            _mark_fired(key, side, diff)
            fired += 1
            _fire(match, row, side, diff, "easy_win", minute)

    return {"checked": checked, "fired": fired}
