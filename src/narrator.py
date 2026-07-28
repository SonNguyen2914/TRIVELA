"""The narrator — the live read, written down.

Turns each live_auto cycle into Discord prose on the DETAIL channel: a
periodic live brief (score, momentum, levers, board vs market, Son's
tracked positions), plus immediate rich analyses on goals, red cards and
phase changes. Template-based on purpose: every number quoted comes
straight from the cycle that triggered it, nothing is generated.

The ACTION channel keeps its terse pings (signals, tracker flips); the
narrator never posts there.
"""
from __future__ import annotations

import time

import config
from src.alerts import send_alert

# match_id -> {"ts": last brief time, "score": str, "phase": str,
#              "reds": (h, a)} — in-memory; a restart costs one extra brief.
_state: dict[str, dict] = {}

_KEY_ROWS = (("home_win", "{home} win90"), ("draw", "draw"),
             ("away_win", "{away} win90"), ("home_advance", "{home} adv"),
             ("away_advance", "{away} adv"), ("over_2_5", "over 2.5"))


def _fmt_board(match, rows_by_key: dict) -> str:
    parts = []
    for key, label in _KEY_ROWS:
        r = rows_by_key.get(key)
        if not r:
            continue
        p = r.get("live_model_probability")
        c = r.get("market_probability")
        if p is None:
            continue
        lab = label.format(home=match.home, away=match.away)
        parts.append(f"{lab} {p:.0%}" + (f" (mkt {c:.0%})" if c is not None else ""))
    return " · ".join(parts)


def _money(v) -> str:
    """`—` for a figure that does not exist, rather than a crash.

    `cashout_now` is None on a NO_BID position and `hold_ev` is None on a
    STALE_MODEL one. Formatting either with `:.0f` raises TypeError, and
    the caller catches it — so one non-executable position silently took
    the WHOLE narrator brief down instead of printing a dash."""
    return f"${v:.0f}" if isinstance(v, (int, float)) else "—"


def _fmt_positions(positions: list[dict]) -> str:
    if not positions:
        return ""
    lines = ["**Your positions:**"]
    for p in positions:
        lines.append(
            f"  {p['market_title'][:34]} — hold {_money(p['hold_ev'])} / "
            f"cash {_money(p['cashout_now'])} → **{p['verdict']}**")
    return "\n".join(lines)


def narrate(match, out: dict, positions: list[dict]) -> None:
    """One narrator pass for one live match's cycle. Emits an immediate
    detail post on score/red/phase events, else a periodic brief."""
    ls = out.get("live_state") or {}
    score = str(ls.get("score"))
    phase = str(ls.get("phase"))
    minute = ls.get("minutes_elapsed")
    reds = (ls.get("red_home") or 0, ls.get("red_away") or 0)
    rows_by_key = {r.get("outcome_key"): r for r in out.get("markets", [])}
    now = time.time()
    st = _state.get(match.match_id)
    at = f"{minute:.0f}'" if isinstance(minute, (int, float)) else "?"

    event = None
    if st is not None:
        if score != st["score"]:
            event = f"⚽ **GOAL — {match.home} {score} {match.away}** ({at})"
        elif reds != st["reds"]:
            side = match.home if reds[0] > st["reds"][0] else match.away
            event = f"🟥 **RED CARD — {side}** ({at})"
        elif phase != st["phase"]:
            event = f"⏱ **{phase.upper()}** — {match.home} {score} {match.away} ({at})"

    due = st is None or (now - st["ts"]) >= config.NARRATOR_INTERVAL_MINUTES * 60
    if not event and not due:
        return
    _state[match.match_id] = {"ts": now, "score": score, "phase": phase,
                              "reds": reds}

    lv = out.get("levers") or {}
    mom = (lv.get("momentum") or {})
    share = mom.get("recent_share_home")
    mom_line = (f"momentum: {match.home} {share:.0%} of recent threat"
                if isinstance(share, (int, float)) else "")
    head = event or (f"📊 **LIVE BRIEF — {match.home} {score} {match.away}** ({at}, {phase})")
    body = [head]
    if mom_line:
        body.append(mom_line)
    board = _fmt_board(match, rows_by_key)
    if board:
        body.append(board)
    pos = _fmt_positions(positions)
    if pos:
        body.append(pos)
    send_alert("\n".join(body), kind="detail")
