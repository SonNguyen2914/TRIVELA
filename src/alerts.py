"""The alert GATE, and the private transports behind it.

WHY THIS MODULE LOOKS LIKE THIS
===============================
Trivela's hardest invariant is that with `REAL_MONEY_SIGNALS_ENABLED=
false` the platform emits no real-money betting signals. The action
channel reaches Son, and a consenting third party places REAL bets on
what arrives there.

Before this module became a gate that invariant was documentation only.
`src/live_signals.py`, `src/positions.py` and `jobs/scheduler.py`
composed "NEW VALUE BET", "RIPE — BET WINDOW OPEN", "BUY/SELL SIGNAL",
"EASY WIN", "FINAL DECISION LOCKED" and cash-out reads and handed them
straight to the transports. No transport consulted the flag, so setting
it false prevented nothing; the carve-out in `src/live/journal.py
broadcast()` described an intent the rest of the code did not implement.
It was dormant only because `load_schedule()` still points at a finished
tournament — repointing it at MLS would have made it live.

WHAT IS PRIVATE AND WHAT IS EXPORTED, AND WHY BOTH
==================================================
- `_post_discord` / `_post_ntfy` are the TRANSPORTS. They are PRIVATE TO
  THIS MODULE: underscore-prefixed and named nowhere else in the repo.
- `send_alert()` is the GATE, and the ONLY exported dispatch path.

Either measure alone can be walked around, so this module does both. A
public transport sitting beside a gate invites a direct call from a
future author who does not know the gate exists; a private transport
sitting beside a second public sender invites the wrong sender. Private
transports plus exactly one exported entry point leaves no accidental
route. `tests/test_alert_gate.py` enforces that statically: no module
outside this file may REFERENCE the transports, the webhook settings or
the ntfy topic, and every `send_alert()` call site must declare its
class. The scan is structural — it parses each module and looks for
actual references, imports and settings lookups — so documentation is
free to name any of them. It has to be: explaining why this gate exists
requires naming what it protects, and a guard that punished its own
runbook would be edited down until it stopped guarding.

CLASSIFICATION IS A PROPERTY OF THE CALL SITE
=============================================
`dispatch_class` is a REQUIRED keyword argument with no default, so a
new call site cannot dispatch without stating what it is dispatching.
The gate never inspects the message text: text is what a model writes,
and a rule keyed on wording is a rule an unlucky wording defeats.

    OPERATIONAL     telemetry ABOUT THE PLATFORM — readiness, storage
                    headroom, the channel probe, "the T-10 sweep ran".
                    Carries no model output and no market view. NOT
                    governed by the money lock: silencing this class is
                    how the DiskFull incident stayed invisible behind
                    {"created": 0}.
    AMBIENT_DETAIL  the narrator's live briefs. These do carry model
                    numbers, so the gate pins them to the DETAIL channel
                    — never the act-now channel, never the phone.
    SESSION_RELAY   the documented carve-out (AGENTS.md §3): prose a
                    human analyst session relays to Son. Requires a live
                    capability from `src/live/session_capability.py` —
                    verified against the server-side store, not claimed.
    MODEL_SIGNAL    computed betting content: edges, recommendations,
                    BUY/SELL, ripeness, cash-out reads. This is exactly
                    what the money lock governs, so it dispatches only
                    when REAL_MONEY_SIGNALS_ENABLED is true.

Where the line falls between OPERATIONAL and MODEL_SIGNAL: an
operational message says something about the PLATFORM ("the sweep ran",
"the volume is 85% full", "collection has stopped"). A model signal says
something about a MARKET ("this contract is mispriced", "sell now").
A message that would change what somebody bets is the second kind, and
the "shadow, not advice" label on it is honour-system, not a mechanism.

Every refusal is printed AND recorded in a ring buffer readable through
`recent_refusals()` / `refusal_counts()`, and over HTTP at
`GET /api/admin/alerts/refusals`. A gate that refuses silently is
indistinguishable from a transport that is down. The buffer is
process-local on purpose — same assumption as the session-capability
store and the rate limiter — and a restart losing it is acceptable
because the printed line is the durable record.

CREDENTIAL HYGIENE
==================
A Discord webhook URL and an ntfy topic ARE credentials: anyone holding
either can post into Son's channel. `requests` exceptions routinely
embed the request URL, so printing one verbatim publishes the credential
into the logs (and the previous no-webhook branch additionally printed
the first 80 characters of the message body). Transport failures
therefore log the transport NAME and a sanitized error class — plus an
HTTP status when the exception carries a response — and never the URL,
the topic, the webhook path, or any part of the message.
"""
from __future__ import annotations

import inspect
from collections import deque
from datetime import datetime, timezone

import requests

import config

# The per-transport payload ceiling, in characters (journal-P0-A).
# Discord hard-caps content at 2000; ntfy is more generous. 1900 is the
# historical safety margin. It is a NAMED constant because a caller
# composing a message with a mandatory tail (the shadow qualifier) must
# reserve space against the SAME number the transports truncate at —
# two different literals here and in the composer is exactly how a
# truncation silently eats the tail.
TRANSPORT_MESSAGE_LIMIT = 1900

# --- dispatch classes ------------------------------------------------------
# Declared by the CALL SITE. See the module docstring for what each one
# means and why the gate never reads the message text.
OPERATIONAL = "operational"
AMBIENT_DETAIL = "ambient_detail"
SESSION_RELAY = "session_relay"
MODEL_SIGNAL = "model_signal"
DISPATCH_CLASSES = (OPERATIONAL, AMBIENT_DETAIL, SESSION_RELAY,
                    MODEL_SIGNAL)

# --- the refusal ledger ----------------------------------------------------
_REFUSALS_KEPT = 200
_refusals: deque = deque(maxlen=_REFUSALS_KEPT)
_refusal_counts: dict[str, int] = {}


def _reset_refusals_for_tests() -> None:
    """Drop the ledger. Test-only helper; no runtime caller, no route."""
    _refusals.clear()
    _refusal_counts.clear()


def _gate_origin() -> str:
    """The first frame outside this module — the CALL SITE that was
    refused. Computed only on the refusal path, so the stack walk costs
    nothing in the normal case. A refusal that cannot name its origin is
    a log line nobody can act on."""
    try:
        for fi in inspect.stack(0)[1:]:
            mod = fi.frame.f_globals.get("__name__") or ""
            if mod and mod != __name__:
                return f"{mod}:{fi.function}"
    except Exception:                      # introspection must never raise
        pass
    return "unknown"


def _record_refusal(dispatch_class: str, kind: str, reason: str,
                    origin: str) -> dict:
    entry = {"at": datetime.now(timezone.utc).isoformat(),
             "dispatch_class": dispatch_class, "kind": kind,
             "origin": origin, "reason": reason,
             "real_money_signals_enabled":
                 config.REAL_MONEY_SIGNALS_ENABLED}
    _refusals.append(entry)
    _refusal_counts[dispatch_class] = _refusal_counts.get(
        dispatch_class, 0) + 1
    # printed as well as recorded: the ring buffer dies with the process,
    # the log line does not
    print(f"[alert-gate] REFUSED {dispatch_class} from {origin} "
          f"(kind={kind}): {reason}")
    return entry


def recent_refusals(limit: int = 50) -> list[dict]:
    """The most recent refusals, newest first. No message content — a
    refused betting signal must not be readable from the observability
    surface either."""
    rows = list(_refusals)[-max(int(limit), 0):]
    return list(reversed(rows))


def refusal_counts() -> dict:
    """Refusals per dispatch class since this process started."""
    return dict(_refusal_counts)


# --- the transports (PRIVATE to this module) -------------------------------

def _transport_failure(transport: str, exc: BaseException) -> str:
    """A log line naming the transport and the error CLASS only.

    Never `str(exc)`: a `requests` exception embeds the request URL, and
    for these two transports the URL *is* the credential — the Discord
    webhook path and the ntfy topic both grant write access to Son's
    channel to anyone who holds them. An HTTP status is included when
    the exception carries a response, because a status is diagnostic and
    is not a secret."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    tail = f" (HTTP {status})" if isinstance(status, int) else ""
    return (f"[alert] {transport} transport failed: "
            f"{type(exc).__name__}{tail}")


def _post_discord(message: str, channel: str = "action") -> bool:
    """PRIVATE transport. Post to one of the two Discord channels:
    "action" (terse, act-now) or "detail" (the narrator's full briefs).
    Both fall back to the single DISCORD_WEBHOOK_URL when the split isn't
    configured. Reached only from inside this module — callers use
    `send_alert()`."""
    url = (config.DISCORD_DETAIL_WEBHOOK_URL if channel == "detail"
           else config.DISCORD_ACTION_WEBHOOK_URL)
    if not url:
        # no message body: this used to print message[:80], which put
        # relayed prose into the logs of a system whose whole point is
        # controlling where that prose goes
        print(f"[alert] discord/{channel} not configured — nothing sent")
        return False
    try:
        resp = requests.post(
            url, json={"content": message[:TRANSPORT_MESSAGE_LIMIT]},
            timeout=8)
        return resp.status_code in (200, 204)
    except requests.RequestException as exc:
        print(_transport_failure(f"discord/{channel}", exc))
        return False


def _post_ntfy(message: str, title: str = "WC26",
               priority: str = "high") -> bool:
    """PRIVATE transport. Instant phone push via ntfy.sh — no account, no
    open page, no RC. Silently no-ops when the topic is unset. Reached
    only from inside this module — callers use `send_alert()`."""
    if not config.NTFY_TOPIC:
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{config.NTFY_TOPIC}",
            data=message[:TRANSPORT_MESSAGE_LIMIT].encode("utf-8"),
            headers={"Title": title, "Priority": priority,
                     "Tags": "soccer"},
            timeout=8)
        return resp.status_code == 200
    except requests.RequestException as exc:
        print(_transport_failure("ntfy", exc))
        return False


# --- the gate --------------------------------------------------------------

def _authorize(dispatch_class: str, kind: str, capability) -> str | None:
    """The refusal reason, or None when the dispatch is authorized.

    Fails closed: an unrecognised class is refused rather than waved
    through, so adding a class without teaching the gate about it stops
    dispatch instead of silently opening one."""
    if dispatch_class not in DISPATCH_CLASSES:
        return (f"unknown dispatch_class {dispatch_class!r} — the gate "
                f"fails closed on a class it does not recognise; valid "
                f"classes are {DISPATCH_CLASSES}")
    if dispatch_class == OPERATIONAL:
        return None
    if dispatch_class == AMBIENT_DETAIL:
        if kind != "detail":
            return ("ambient narration may ride the detail channel only "
                    f"— this call asked for kind={kind!r}, which would "
                    f"put model numbers on the act-now channel and the "
                    f"phone")
        return None
    if dispatch_class == SESSION_RELAY:
        from src.live import session_capability
        if session_capability.is_live(capability) is None:
            return ("session relay requires a LIVE interactive-session "
                    "capability; none was presented, or the one "
                    "presented is not in the server-side store. The "
                    "carve-out covers a human session speaking, and "
                    "that has to be a verified fact")
        return None
    # MODEL_SIGNAL
    if not config.REAL_MONEY_SIGNALS_ENABLED:
        return ("model-generated betting content is refused while "
                "REAL_MONEY_SIGNALS_ENABLED=false — the flag governs "
                "exactly this class, and a consenting third party bets "
                "real money on what reaches this channel")
    return None


def send_alert(message: str, title: str = "WC26", kind: str = "action",
               *, dispatch_class: str, capability=None) -> dict[str, bool]:
    """The ONLY exported dispatch path. Authorize, then fan out by kind.

    "action": the act-now channel + phone push, and a copy to detail so
    that channel reads as a complete log. "detail": the narrator's
    channel only — the phone stays quiet.

    `dispatch_class` is required and has no default; see the module
    docstring. A refused call performs ZERO transport work — the refusal
    is decided before any URL is read — and returns an EMPTY dict, so
    the per-transport contract below is preserved: a caller reporting
    delivery reads an empty result as "nothing was accepted", which is
    true.

    Returns PER-TRANSPORT acceptance (journal-P1 F6): each key is a
    transport, each value the transport's own boolean. It used to return
    None, which forced callers to report "dispatched without raising" as
    if it were delivery — a small lie in a system whose whole point is
    not telling those. Existing fire-and-forget callers ignore the
    return value, which is fine; anyone REPORTING dispatch must read it.
    """
    refusal = _authorize(dispatch_class, kind, capability)
    if refusal is not None:
        _record_refusal(dispatch_class, kind, refusal, _gate_origin())
        return {}
    if kind == "detail":
        return {"discord_detail": _post_discord(message, channel="detail")}
    out = {"discord_action": _post_discord(message, channel="action")}
    if config.DISCORD_DETAIL_WEBHOOK_URL != config.DISCORD_ACTION_WEBHOOK_URL:
        out["discord_detail"] = _post_discord(message, channel="detail")
    out["ntfy"] = _post_ntfy(message, title=title)
    return out


# --- operational self-test -------------------------------------------------

def channel_probe() -> dict:
    """Fire a fixed test message down each transport leg and report
    PER-LEG delivery, so a silent failure (bad webhook, mistyped ntfy
    topic, whitespace in an env var) is visible in the response instead
    of only in server logs.

    It lives HERE rather than in the route because the transports are
    private to this module: the route asks the gate module to probe
    itself instead of reaching past it. The three messages are module
    constants — this is not a general-purpose sender, and no caller
    supplies text to it. Each leg is exercised directly because the
    fan-out copy-to-detail is `send_alert`'s concern, not this probe's.
    """
    a = _post_discord("⚡ ACTION channel test — act-now pings (signals, "
                      "tracker flips, goals) arrive here.",
                      channel="action")
    d = _post_discord("📊 DETAIL channel test — the narrator posts live "
                      "briefs, goal analyses and your position table "
                      "here.", channel="detail")
    n = _post_ntfy("📱 ntfy path test — if this reached your phone, the "
                   "push loop is closed.", title="WC26 channel test")
    return {"sent": True,
            "action_configured": bool(config.DISCORD_ACTION_WEBHOOK_URL),
            "detail_configured": bool(config.DISCORD_DETAIL_WEBHOOK_URL),
            "split": (config.DISCORD_ACTION_WEBHOOK_URL
                      != config.DISCORD_DETAIL_WEBHOOK_URL),
            "ntfy_configured": bool(config.NTFY_TOPIC),
            "action_delivered": a,
            "detail_delivered": d,
            "ntfy_delivered": n}


# --- model-signal composers ------------------------------------------------
# Each of these composes a betting recommendation from model output. They
# are MODEL_SIGNAL by construction, so with the money lock false the gate
# refuses them and no transport is touched. They are kept (rather than
# deleted) because the WC26 scheduler still calls them and because the
# refusal — logged, counted, attributable to a call site — is better
# evidence than an absent function.

def alert_new_take(match_name: str, market_title: str, edge: float,
                   ev: float) -> None:
    send_alert(
        f"🟢 **NEW VALUE BET** — {match_name}\n"
        f"{market_title}\nEdge: {edge:+.1%} | EV per $1: {ev:+.2f}",
        dispatch_class=MODEL_SIGNAL)


def alert_ripe(match_name: str, market_title: str, timing: dict) -> None:
    reasons = "\n".join(f"• {r}" for r in timing["reasons"][:4])
    send_alert(
        f"⏰ **RIPE — BET WINDOW OPEN** ({timing['score']:.0f}/100) — "
        f"{match_name}\n"
        f"**{market_title}** @ {timing['current_odds']:.2f} "
        f"(edge {timing['current_edge']:+.1%})\n{reasons}",
        dispatch_class=MODEL_SIGNAL)


def alert_final_lock(match_name: str, best_bet: dict | None) -> None:
    if best_bet:
        send_alert(
            f"🔴 **FINAL DECISION LOCKED** (T-10min) — {match_name}\n"
            f"Best bet: {best_bet['market_title']}\n"
            f"Model: {best_bet['model_probability']:.0%} | "
            f"Odds: {best_bet['decimal_odds']} | "
            f"EV: {best_bet['expected_value']:+.2f} → "
            f"**{best_bet['recommendation']}**",
            dispatch_class=MODEL_SIGNAL)
    else:
        send_alert(f"🔴 **FINAL DECISION LOCKED** — {match_name}: no bets "
                   f"clear the bar. SKIP all.",
                   dispatch_class=MODEL_SIGNAL)
