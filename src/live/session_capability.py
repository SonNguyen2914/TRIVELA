"""Interactive-session capability for action-channel dispatch
(journal-P0-G).

WHY THIS EXISTS
===============
The action channel reaches Son, and a consenting friend places REAL
bets on what he relays. Round two made `source="session"` a verified
fact for IN-PROCESS callers by inspecting the call stack — a parameter
can be forged, a stack frame cannot.

That defence never bound the HTTP path. Over HTTP the stack is
uvicorn → fastapi → api.main, which contains no scheduler or model
frame, so ANY client holding the generic operator token could POST
`source="session"` and dispatch to Discord/ntfy. The check was one
layer short of the boundary it was written to protect.

So dispatch now requires a CAPABILITY: a short-lived server-side
artifact that the generic admin token alone cannot produce.

    POST /api/admin/mls/session/challenge   (admin token)
        -> {challenge_id, nonce}                   server-issued nonce

    POST /api/admin/mls/session/open        (admin token +
                                             HMAC(secret, nonce))
        -> {session_token, expires_at}             the capability

    POST /api/admin/mls/broadcast           (admin token +
                                             X-Session-Token)

`JOURNAL_SESSION_SECRET` is the second factor and must differ from
`ADMIN_TOKEN`. It never crosses the wire — the client proves possession
by HMAC over a nonce the server issued and remembers, so the response
is neither replayable nor forgeable from a captured request.

FAIL-CLOSED PROPERTIES
======================
- secret unset            -> no capability can ever be minted
- secret == ADMIN_TOKEN   -> refused; that would collapse two factors
                             into one and restore exactly the defect
- unknown/expired/reused challenge -> refused
- capability expired or not in the live store -> refused

Capabilities live in PROCESS MEMORY, deliberately. A restart revokes
every live session, which errs closed; persisting them would let a
capability outlive the interactive session that justified it. This
matches the rate limiter's existing single-worker assumption.

The raw token is never retained — only its SHA-256 — and every
comparison is constant-time.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config


def _now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SessionCapability:
    """Proof that an explicitly authorised interactive session is
    speaking. Constructing one of these directly proves nothing: every
    consumer re-checks it against the live store (`is_live`), so only a
    capability this module actually MINTED can dispatch."""
    id: str
    label: str | None
    issued_at: datetime
    expires_at: datetime

    def as_dict(self) -> dict:
        return {"capability_id": self.id, "session_label": self.label,
                "issued_at": self.issued_at.isoformat(),
                "expires_at": self.expires_at.isoformat()}


# challenge_id -> {"nonce", "expires_at"}; a challenge is single-use and
# is removed the moment it is answered, right or wrong.
_challenges: dict[str, dict] = {}
# sha256(token) -> {"capability": SessionCapability}
_capabilities: dict[str, dict] = {}


def _reset_for_tests() -> None:
    """Drop all challenges and capabilities. Test-only helper — there is
    no runtime caller and no route that reaches it."""
    _challenges.clear()
    _capabilities.clear()


def _prune(now=None) -> None:
    now = now or _now()
    for cid in [k for k, v in _challenges.items()
                if v["expires_at"] <= now]:
        _challenges.pop(cid, None)
    for key in [k for k, v in _capabilities.items()
                if v["capability"].expires_at <= now]:
        _capabilities.pop(key, None)


def secret_status() -> dict:
    """Whether an interactive session can be opened at all, and why not
    when it cannot. Reported rather than inferred — an operator whose
    handshake fails must be told which factor is missing, and 'not
    configured' must never read as 'authorised'."""
    if not config.JOURNAL_SESSION_SECRET:
        return {"available": False,
                "reason": ("JOURNAL_SESSION_SECRET is unset — no "
                           "interactive session can be opened, so the "
                           "action channel has no dispatch path")}
    if not config.ADMIN_TOKEN:
        return {"available": False,
                "reason": ("ADMIN_TOKEN is unset — operator "
                           "authentication is disabled entirely")}
    if hmac.compare_digest(config.JOURNAL_SESSION_SECRET,
                           config.ADMIN_TOKEN):
        return {"available": False,
                "reason": ("JOURNAL_SESSION_SECRET equals ADMIN_TOKEN — "
                           "that collapses the two factors into one and "
                           "would let the generic operator token mint a "
                           "session capability by itself; refused")}
    return {"available": True, "reason": None}


def challenge() -> dict:
    """Issue a single-use nonce for an operator to sign. Returns an
    error dict when no session could be opened anyway — telling the
    operator now is better than a handshake that fails at step two."""
    status = secret_status()
    if not status["available"]:
        return {"error": status["reason"]}
    _prune()
    cid = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(32)
    _challenges[cid] = {
        "nonce": nonce,
        "expires_at": _now() + timedelta(
            seconds=config.JOURNAL_CHALLENGE_TTL_SECONDS)}
    return {"challenge_id": cid, "nonce": nonce,
            "algorithm": "HMAC-SHA256(JOURNAL_SESSION_SECRET, nonce)",
            "expires_in_seconds": config.JOURNAL_CHALLENGE_TTL_SECONDS}


def expected_response(nonce: str) -> str:
    """The signature a holder of the session secret would produce. Used
    by the handshake and by tests; it is NOT a route."""
    return hmac.new(config.JOURNAL_SESSION_SECRET.encode("utf-8"),
                    nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def open_session(challenge_id: str, response: str,
                 label: str | None = None) -> dict:
    """Answer a challenge and mint a capability.

    The challenge is consumed whether or not the answer is right, so a
    captured nonce cannot be brute-forced or replayed."""
    status = secret_status()
    if not status["available"]:
        return {"error": status["reason"]}
    _prune()
    ch = _challenges.pop(challenge_id or "", None)
    if ch is None:
        return {"error": ("unknown, expired or already-answered "
                          "challenge — request a new one")}
    if not response or not hmac.compare_digest(
            str(response), expected_response(ch["nonce"])):
        return {"error": ("challenge response does not verify — the "
                          "session secret is a second factor the "
                          "operator token cannot substitute for")}
    now = _now()
    cap = SessionCapability(
        id=secrets.token_urlsafe(12), label=label, issued_at=now,
        expires_at=now + timedelta(
            seconds=config.JOURNAL_SESSION_TTL_SECONDS))
    token = secrets.token_urlsafe(32)
    _capabilities[hashlib.sha256(token.encode()).hexdigest()] = {
        "capability": cap}
    out = cap.as_dict()
    out["session_token"] = token
    return out


def verify(token: str | None) -> SessionCapability | None:
    """The capability a bearer token names, or None. Constant-time
    lookup by digest; the raw token is never stored."""
    if not token:
        return None
    _prune()
    entry = _capabilities.get(
        hashlib.sha256(str(token).encode()).hexdigest())
    if entry is None:
        return None
    cap = entry["capability"]
    return cap if cap.expires_at > _now() else None


def close_session(token: str | None) -> dict:
    """End a session early. An operator who thinks a token leaked must
    be able to revoke it without waiting out the TTL or restarting the
    service."""
    if not token:
        return {"closed": False}
    key = hashlib.sha256(str(token).encode()).hexdigest()
    return {"closed": _capabilities.pop(key, None) is not None}


def is_live(capability) -> SessionCapability | None:
    """Re-check a capability OBJECT against the live store.

    This is what makes the artifact server-side rather than caller-
    supplied: a caller can construct a `SessionCapability` — it is an
    ordinary dataclass — but cannot put one into the store without
    answering a challenge, and this returns None for anything the store
    does not hold."""
    if not isinstance(capability, SessionCapability):
        return None
    _prune()
    for entry in _capabilities.values():
        held = entry["capability"]
        if hmac.compare_digest(held.id, capability.id):
            return held if held.expires_at > _now() else None
    return None
