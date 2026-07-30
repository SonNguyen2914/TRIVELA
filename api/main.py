"""FastAPI app — the on-demand layer.

Endpoints
  GET  /api/health
  GET  /api/matches/upcoming?hours_ahead=48
  GET  /api/suggestions                      likelihood ranking board (tiered)
  GET  /api/prediction/{match_id}            cached (or fresh if stale/missing)
  GET  /api/prediction/{match_id}?force_refresh=true
  GET  /api/prediction/{match_id}/timeline   how one outcome evolved
  POST /api/prediction/{match_id}/refresh    force a fresh run (one match)
  POST /api/prediction/{match_id}/live       price markets vs a live state
  GET  /api/prediction/{match_id}/live-state  auto-fetch live state (feed)
  GET  /api/live-feed/budget                  API-Football calls remaining today
  POST /api/refresh-all                      force fresh runs (all trackable)
  GET  /api/settings                         current thresholds
  POST /api/settings                         update thresholds
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select

import config
from src.cache import latest_for_match, timeline_for_match
from src.db import (SessionLocal, get_setting, init_db,
                    set_setting, utcnow)
from src.live_feed import budget_status, live_state_for
from src.model_cache import refresh_model_cache
from src.schedule_data import (get_match, get_team_stats, has_sourced_stats,
                               is_trackable, load_schedule, provisional_teams)
from src.suggester import SuggesterEngine
from src import spike_detector
from src import live_state as live_state_svc
from src.bracket import bracket_status

app = FastAPI(title="Kalshi WC26 Bet Suggester", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-process last-fired timestamps for the expensive-route rate limit.
# Process-local by design (single-worker deployment); a restart resets it,
# which only ever errs permissive for one call.
_rate_last: dict[str, float] = {}

# Route prefixes whose recomputation is expensive enough to rate-limit
# even for reads (V9.3 eval F20). Each of these can fan out simulations,
# full-table reads, replay, or provider calls, so an unauthenticated
# caller could otherwise burn CPU, database and provider quota at will.
# The mutation surface was already fail-closed; these are the READS.
# NOTE the limiter buckets per PREFIX, not per path, so only SINGLETON
# routes belong here. Per-match routes (e.g. /api/research/{id}) must not
# be listed: one visitor opening two different matches would 429 the
# second, which is a correctness bug, not protection.
_EXPENSIVE_PREFIXES = (
    "/api/refresh-all",
    "/api/mls/model-eval",     # rolling-origin ladder + bootstrap
    "/api/mls/corpus",         # full-corpus assembly / download
    "/api/mls/audit",          # per-lock replay + hash recomputation
)


def _admin_ok(request) -> bool:
    """Operator credential check: X-Admin-Token or Authorization: Bearer,
    compared constant-time. Empty configured token or empty/malformed
    request credentials always fail — an unset ADMIN_TOKEN disables
    operator mutations entirely rather than matching an empty header."""
    import secrets as _secrets
    token = request.headers.get("x-admin-token", "")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return (bool(config.ADMIN_TOKEN) and bool(token)
            and _secrets.compare_digest(token, config.ADMIN_TOKEN))


@app.middleware("http")
async def _public_guard(request, call_next):
    """Post-tournament lockdown (Jul 21 evaluation, P0):

    - PUBLIC_READ_ONLY=true rejects every mutating verb with 403 unless
      the request carries the server-held ADMIN_TOKEN header. The token
      lives only in the deployment environment and operator tooling —
      NEVER in the browser bundle, which is why this is a header check
      rather than anything cookie- or client-config-based.
    - Expensive recompute routes are rate-limited per process regardless
      of mode (RATE_LIMIT_SECONDS apart), 429 otherwise.
    """
    import time as _t

    from fastapi.responses import JSONResponse

    path = request.url.path
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and config.PUBLIC_READ_ONLY:
        # Auth is evaluated BEFORE the rate bucket so an unauthenticated
        # caller can never exhaust the limiter and lock the operator out.
        if not _admin_ok(request):
            return JSONResponse(
                {"detail": "read-only mode: the tournament is over; "
                           "mutations require operator credentials"},
                status_code=403)
    if config.RATE_LIMIT_SECONDS > 0:      # <=0 disables (tests, dev)
        for prefix in _EXPENSIVE_PREFIXES:
            if path.startswith(prefix):
                now = _t.monotonic()
                last = _rate_last.get(prefix)
                if last is not None and now - last < config.RATE_LIMIT_SECONDS:
                    return JSONResponse(
                        {"detail": "rate limited: expensive route"},
                        status_code=429)
                _rate_last[prefix] = now
    return await call_next(request)


engine = SuggesterEngine()


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "demo_mode": config.DEMO_MODE, "time": utcnow().isoformat()}


# --- MLS (next-league data layer; read-only, no DB) ------------------------
@app.get("/api/mls/scoreboard")
def mls_scoreboard(date: str | None = Query(None, pattern=r"^\d{8}$")):
    from src import mls
    return {"fixtures": mls.scoreboard(date), "generated_at": utcnow().isoformat()}


@app.get("/api/mls/schedule")
def mls_schedule(days: int = Query(7, ge=1, le=14)):
    from src import mls
    return {"fixtures": mls.schedule(days), "generated_at": utcnow().isoformat()}


@app.get("/api/mls/standings")
def mls_standings():
    from src import mls
    return {"conferences": mls.standings(), "generated_at": utcnow().isoformat()}


@app.get("/api/mls/markets")
def mls_markets():
    from src import mls
    return {"games": mls.game_books(), "cup": mls.cup_futures(),
            "generated_at": utcnow().isoformat()}


@app.get("/api/mls/match/{event_id}")
def mls_match(event_id: str):
    from src import mls
    if not event_id.isdigit() or len(event_id) > 12:
        raise HTTPException(404, "unknown event")
    out = mls.match_summary(event_id)
    if out is None:
        raise HTTPException(502, "summary unavailable")
    book = None
    books = []
    try:
        books = mls.find_all_books(
            out.get("date"),
            (out.get("home") or {}).get("name") or "",
            (out.get("away") or {}).get("name") or "")
        # legacy shape (deploy-skew safety): the winner family alone
        book = next((f for f in books if f.get("key") == "winner"), None)
    except Exception as exc:            # the hub must not die on the book
        print(f"[mls] book match failed for {event_id}: {exc}")
    model = None
    try:                                # nor on the live plane
        from src.live import runs as live_runs
        model = live_runs.model_for_event(event_id)
    except Exception as exc:
        print(f"[mls] model section failed for {event_id}: {exc}")
    lineup = None
    try:                # nor on the lineup view (display context only)
        from src import mls as _mls
        from src.live import lineup_view
        raw = _mls.raw_summary(event_id)
        if raw:
            lineup = lineup_view.build(raw)
    except Exception as exc:
        print(f"[mls] lineup section failed for {event_id}: {exc}")
    return {"match": out, "book": book, "books": books, "model": model,
            "lineups": lineup, "generated_at": utcnow().isoformat()}


@app.get("/api/mls/briefing/{event_id}")
def mls_briefing(event_id: str):
    """Everything needed to reason about one fixture RIGHT NOW, in one
    call — built for a live session rather than a human.

    A session opening cold mid-match would otherwise spend its first
    turns assembling state from several endpoints while the match moves.
    The frozen T-10 book and the current book sit side by side, each
    labelled, because comparing the wrong one to the wrong one is how a
    bad hold gets justified. The standing edge travels with its interval
    and significance flag so it cannot be quoted without them.

    Public read-only; exposes nothing `/api/mls/match/{id}` does not,
    plus the journal. 20s cache."""
    if not event_id.isdigit() or len(event_id) > 12:
        raise HTTPException(404, "unknown event")
    from src.mls import _cached
    try:
        from src.live import journal
        return _cached(f"mls_briefing_{event_id}", 20,
                       lambda: journal.briefing(event_id)) or {}
    except Exception as exc:
        print(f"[mls] briefing failed for {event_id}: {exc}")
        raise HTTPException(503, "briefing unavailable")


@app.get("/api/mls/journal")
def mls_journal(fixture_id: int | None = Query(None)):
    """The personal journal with its DENOMINATOR — considered, taken and
    passed. A hit rate over taken bets alone is not a statistic.

    Below the policy minimum this returns rows and NO summary statistic;
    the keys are absent rather than zero, because a zero reads as a
    measured result. Scoped to the MLS competition (journal-P1 F9).
    Public read-only."""
    try:
        from src.live import journal
        return journal.journal_summary(fixture_id,
                                       competition_slug="mls-2026")
    except Exception as exc:
        print(f"[mls] journal failed: {exc}")
        raise HTTPException(503, "journal unavailable")


# Journal mutation payloads travel as typed JSON bodies (journal-P1 F8).
# They used to ride the query string, which URL-decodes: a rationale
# containing `&`, `%`, newlines or unencoded Unicode was silently
# truncated or mangled — in the one table whose whole value is verbatim
# provenance. JSON round-trips the text exactly.
class JournalViewIn(BaseModel):
    # No `status` field (journal-P0-D): every view starts `considered`.
    # The old default was a default, not a constraint — a caller could
    # post status="taken" and skip the considered→resolve lifecycle
    # entirely. There is no spelling of a pre-resolved entry at any
    # layer now; a posted `status` is ignored, never honoured.
    fixture_id: int
    market_ticker: str
    outcome_key: str | None = None
    stated_price: str | None = None
    stated_size: str | None = None
    market_quote_id: int | None = None
    rationale: str | None = None
    corrects_bet_id: int | None = None


class JournalResolveIn(BaseModel):
    bet_id: int
    status: str


class JournalExecutionIn(BaseModel):
    bet_id: int
    account_label: str
    # REQUIRED and operator-supplied (journal-P0 F5): the server used to
    # stamp utcnow() here, manufacturing the provenance of a third
    # party's consent. The operator states when consent was recorded;
    # the server refuses to invent it.
    consent_recorded_at: datetime
    status: str = "filled"
    fill_price: str | None = None
    filled_contracts: str | None = None
    fee_paid: str | None = None
    filled_at: datetime | None = None
    market_quote_id_at_fill: int | None = None
    not_filled_reason: str | None = None
    best_available_price: str | None = None
    exchange_order_id: str | None = None
    publication_consent: bool = False


class JournalSettlementIn(BaseModel):
    execution_id: int
    # the exchange's own numbers, operator-supplied — never invented
    settlement_credit: str
    settled_at: datetime
    settled_outcome: str | None = None


class JournalReconcileIn(BaseModel):
    execution_id: int
    note: str
    publication_consent: bool | None = None


class BroadcastIn(BaseModel):
    message: str
    channel: str = "action"
    source: str = "session"
    session_label: str | None = None
    fixture_id: int | None = None


class SessionOpenIn(BaseModel):
    challenge_id: str
    # HMAC-SHA256(JOURNAL_SESSION_SECRET, nonce), hex
    response: str
    session_label: str | None = None


def _journal_write(result):
    """Journal mutation responses, with a payload CONFLICT surfaced as
    HTTP 409 (journal-P0-I).

    A conflicting retry must not read as success at any layer. The
    handler already refuses to write; returning it inside a 200 body
    would leave a client that checks only the status code believing its
    corrected economics had been accepted."""
    from fastapi.responses import JSONResponse
    if isinstance(result, dict) and result.get("conflict"):
        return JSONResponse(status_code=409, content=result)
    return result


@app.post("/api/admin/mls/session/challenge")
def mls_session_challenge(request: Request):
    """Operator-only: step one of the interactive-session handshake
    (journal-P0-G).

    Returns a single-use nonce for the operator to sign with
    JOURNAL_SESSION_SECRET. The secret is a SECOND factor and never
    crosses the wire; the operator token alone cannot mint a capability,
    which is precisely the hole this closes — the round-2 stack-frame
    check bound in-process callers only, so over HTTP any token holder
    could claim `source="session"` and reach the channel Son's friend
    reads."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import session_capability
    out = session_capability.challenge()
    if "error" in out:
        raise HTTPException(503, out["error"])
    return out


@app.post("/api/admin/mls/session/open")
def mls_session_open(request: Request, body: SessionOpenIn):
    """Operator-only: step two — answer the challenge, receive a
    short-lived capability token. Present it as `X-Session-Token` on
    /api/admin/mls/broadcast. Capabilities live in process memory, so a
    restart revokes every live session; that errs closed."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import session_capability
    out = session_capability.open_session(
        body.challenge_id, body.response, body.session_label)
    if "error" in out:
        raise HTTPException(403, out["error"])
    return out


@app.post("/api/admin/mls/session/close")
def mls_session_close(request: Request):
    """Operator-only: end an interactive session early. An operator who
    thinks a session token leaked must not have to wait out the TTL."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import session_capability
    return session_capability.close_session(
        request.headers.get("x-session-token"))


@app.get("/api/admin/mls/broadcasts")
def mls_admin_broadcasts(request: Request,
                         fixture_id: int = Query(...),
                         limit: int = Query(20, ge=1, le=200)):
    """Operator-only: what has already been said about a fixture — the
    full broadcast record including the wire payload and per-transport
    acceptance (journal-P0-A/P0-C). Broadcast prose is operator
    content (the ops loop announces fills through it), so it never
    rides the public briefing; a session picks up its thread HERE."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return {"broadcasts": journal.recent_broadcasts(fixture_id, limit),
            "generated_at": utcnow().isoformat()}


@app.get("/api/admin/mls/journal")
def mls_admin_journal(request: Request,
                      fixture_id: int | None = Query(None)):
    """Operator-only: the COMPLETE journal record — rationale, account
    labels, order ids, fill economics, consent provenance, gaps,
    settlement and reconciliation state. The public surfaces serve a
    redacted projection (journal-P0 F2); this is where the full record
    lives, behind the token."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return {"entries": journal.full_entries(fixture_id),
            "evidence_class": "personal_journal",
            "generated_at": utcnow().isoformat()}


@app.post("/api/admin/mls/journal/view")
def mls_journal_record_view(request: Request, body: JournalViewIn):
    """Operator-only: record a view AT THE MOMENT IT FORMS.

    Record at `considered`, then resolve to `taken` or `passed`. The
    passes are what make the takes interpretable — a journal of only the
    bets that were taken has the survivorship problem the paper ledger's
    retained rejections exist to prevent.

    Falsifiability is enforced server-side: citing a quote that does not
    exist, or one captured after this moment, downgrades the entry to
    `stated_only`, and it then counts nowhere. A quote that exists but
    belongs to a DIFFERENT fixture, contract or outcome is refused
    outright with the mismatch named (journal-P0 F3)."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return journal.record_view(
        body.fixture_id, body.market_ticker,
        outcome_key=body.outcome_key,
        stated_price=body.stated_price, stated_size=body.stated_size,
        market_quote_id=body.market_quote_id, rationale=body.rationale,
        corrects_bet_id=body.corrects_bet_id)


@app.post("/api/admin/mls/journal/resolve")
def mls_journal_resolve(request: Request, body: JournalResolveIn):
    """Operator-only: resolve a recorded view to `taken` or `passed`.
    A resolution is immutable once set — a correction is a NEW view
    citing `corrects_bet_id`, never a rewrite (journal-P0 F5)."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return journal.resolve_view(body.bet_id, body.status)


@app.post("/api/admin/mls/journal/execution")
def mls_journal_execution(request: Request, body: JournalExecutionIn):
    """Operator-only: record a REAL fill, or a real failure to fill.

    Consent is required and never defaulted — this is a third party's
    money, and the provenance of that consent belongs in the row rather
    than in anyone's memory; the operator supplies the timestamp and the
    server refuses to invent one. A `not_filled` row is as valuable as a
    fill: it is evidence about liquidity, half of what this pilot
    measures."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return _journal_write(journal.record_execution(
        body.bet_id, body.account_label,
        consent_recorded_at=body.consent_recorded_at,
        status=body.status, fill_price=body.fill_price,
        filled_contracts=body.filled_contracts, fee_paid=body.fee_paid,
        filled_at=body.filled_at,
        market_quote_id_at_fill=body.market_quote_id_at_fill,
        not_filled_reason=body.not_filled_reason,
        best_available_price=body.best_available_price,
        exchange_order_id=body.exchange_order_id,
        publication_consent=body.publication_consent))


@app.post("/api/admin/mls/journal/settlement")
def mls_journal_settlement(request: Request, body: JournalSettlementIn):
    """Operator-only: record the exchange's settlement of one real
    execution (filled|partial → settled). Settlement facts are written
    once — a retried identical call is a no-op, a conflicting one is
    refused. The columns existed without any writer (journal-P0 F5);
    this is the writer."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return _journal_write(journal.settle_execution(
        body.execution_id, settlement_credit=body.settlement_credit,
        settled_at=body.settled_at, settled_outcome=body.settled_outcome))


@app.post("/api/admin/mls/journal/reconcile")
def mls_journal_reconcile(request: Request, body: JournalReconcileIn):
    """Operator-only: mark one settled execution as reconciled against
    the exchange's own record (settled → reconciled, the final state).
    Requires settlement first; repeat calls are no-ops. This is also
    where `publication_consent` can be granted explicitly for the
    corpus (journal-P0 F2)."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal
    return _journal_write(journal.reconcile_execution(
        body.execution_id, note=body.note,
        publication_consent=body.publication_consent))


@app.post("/api/admin/mls/broadcast")
def mls_broadcast(request: Request, body: BroadcastIn):
    """Operator-only: a live session speaks to Discord/ntfy.

    The session is the analyser; this is its megaphone. Operator-gated
    rather than a local script so it works from any session — laptop,
    cloud, or a phone over remote control.

    Every broadcast is persisted, so a session that dropped mid-match
    can read what it already said, and so what was claimed live becomes
    part of the record. Only `source="session"` dispatches — a computed
    message is a model-generated signal and is refused server-side
    (journal-P0 F1); the action channel carries the standing-edge
    qualifier appended by the server.

    `source="session"` is now a VERIFIED fact, not a claim
    (journal-P0-G). The operator token authenticates the request; it
    does not establish that a human session is speaking, and any
    automated client holding it could previously dispatch to the channel
    Son's friend reads. An `X-Session-Token` from the interactive-session
    handshake is required, and a request without a live one is refused
    here — no transport is called and nothing is persisted as said.

    Figures should come from a briefing read in the SAME turn, never
    from recall — a confident agent narrating a stale price is the
    failure mode this endpoint makes possible."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import journal, session_capability
    cap = session_capability.verify(
        request.headers.get("x-session-token"))
    if cap is None:
        raise HTTPException(
            403, "action dispatch requires a live interactive-session "
                 "capability: POST /api/admin/mls/session/challenge, sign "
                 "the nonce with JOURNAL_SESSION_SECRET, POST "
                 "/api/admin/mls/session/open, then send the returned "
                 "token as X-Session-Token. The operator token alone "
                 "cannot mint one")
    return journal.broadcast(body.message, channel=body.channel,
                             source=body.source,
                             session_label=body.session_label,
                             fixture_id=body.fixture_id,
                             capability=cap)


# === Club Friendlies — VIEWER surface, additive block, 2026-07-28 ==========
# Read-only proxy + cache over ESPN club.friendly and Kalshi KXCLUBFGAME
# (+ verified TOTAL/BTTS/SPREAD families). Deliberately modelless: no
# model, no shadow plane, no locks, no DB writes, no scheduler jobs —
# and none planned (see src.friendlies.FRAMING). There is NO standings
# route: standings do not exist for friendlies and no table is invented.

@app.get("/api/friendlies/scoreboard")
def friendlies_scoreboard(date: str | None = Query(None, pattern=r"^\d{8}$")):
    from src import friendlies
    return {"fixtures": friendlies.scoreboard(date),
            "framing": friendlies.FRAMING,
            "generated_at": utcnow().isoformat()}


@app.get("/api/friendlies/schedule")
def friendlies_schedule(days: int = Query(7, ge=1, le=14)):
    from src import friendlies
    return {"fixtures": friendlies.schedule(days),
            "framing": friendlies.FRAMING,
            "generated_at": utcnow().isoformat()}


@app.get("/api/friendlies/markets")
def friendlies_markets(date: str | None = Query(None, pattern=r"^\d{8}$")):
    """The scoreboard bucket's fixtures joined to their Kalshi game
    books, each with an EXPLICIT mapping status (mapped / unmapped /
    ambiguous / unresolved_name / no_open_markets / unavailable /
    registry_incomplete) and freshness, plus a books-only census of how
    much friendly surface Kalshi lists beyond ESPN's bucket — with its
    own completeness on the record. Full per-event detector work lives
    in the market hunter, not here."""
    from src import friendlies
    return {"fixtures": friendlies.daily_books(date),
            "listed": friendlies.listed_events_summary(),
            "framing": friendlies.FRAMING,
            "generated_at": utcnow().isoformat()}


@app.get("/api/friendlies/match/{event_id}")
def friendlies_match(event_id: str):
    from src import friendlies
    if not event_id.isdigit() or len(event_id) > 12:
        raise HTTPException(404, "unknown event")
    out = friendlies.match_summary(event_id)
    if out is None:
        raise HTTPException(502, "summary unavailable")
    # Fallback is UNAVAILABLE, not unmapped: if the book section dies we
    # could not look, and "no book exists" is a claim a failure cannot
    # support (P0-2).
    books = {"status": "unavailable", "candidates": [], "freshness": None,
             "families": []}
    try:                                # the page must not die on the book
        books = friendlies.find_all_books(
            out.get("date"),
            (out.get("home") or {}).get("name") or "",
            (out.get("away") or {}).get("name") or "")
    except Exception as exc:
        print(f"[friendlies] book match failed for {event_id}: {exc}")
    return {"match": out, "books": books,
            "framing": friendlies.FRAMING,
            "generated_at": utcnow().isoformat()}


@app.post("/api/admin/mls/sweep")
def mls_admin_sweep(request: Request, force: bool = Query(False)):
    """Operator-only: run the shadow sweeps NOW and return their result
    dicts — the remote eyes for a boot that reports zero runs. force
    regenerates runs regardless of freshness (e.g. after a model or
    payload change). The middleware already enforces the token in
    read-only mode; the explicit check keeps this locked even if that
    mode is ever off."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import ingest as live_ingest
    from src.live import markets as live_markets
    from src.live import runs as live_runs
    return {"window": live_ingest.refresh_window(),
            "map": live_markets.discover_and_map(),
            "runs": live_runs.scheduled_runs(
                freshness_hours=0.0 if force else 4.0),
            "generated_at": utcnow().isoformat()}


@app.post("/api/admin/mls/paper-backfill")
def mls_admin_paper_backfill(request: Request, limit: int = Query(50)):
    """Operator-only: recompute paper signals for canonical locks the
    paper engine never ran for.

    Found on the first prospective slate (2026-07-25): 15 locks carried
    45 quote-linked game legs and the ledger held 27 signals, with no
    metric anywhere reporting the shortfall. The recomputation is
    deterministic — every input is frozen on the lock, including the
    quote age the staleness gate tests — but the recovered rows are
    stamped `backfilled_at` so they can never be mistaken for evidence
    that existed at lock time. PAPER only; no real order path exists."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import paper as live_paper
    return {**live_paper.backfill_uncovered_locks(limit=limit),
            "generated_at": utcnow().isoformat()}


@app.get("/api/admin/mls/storage")
def mls_admin_storage(request: Request):
    """Operator-only, READ-ONLY: what is consuming the live-plane volume.
    Added after a DiskFull incident (Jul 25) in which every prediction
    write failed while the sweep reported only {"created": 0} — there was
    no way to see that the volume was full."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from sqlalchemy import text

    from src.live.db import get_engine
    eng = get_engine()
    if eng is None:
        return {"dormant": True}
    out: dict = {}
    # headroom first: the diagnostic added after the DiskFull incident
    # reported what was consuming the volume but never how close to full
    # it was, so it could not answer the only question that mattered
    try:
        from src.live import observability
        out["headroom"] = observability.storage_headroom()
    except Exception as exc:
        out["headroom"] = {"error": str(exc)}
    with eng.connect() as c:
        try:
            out["database_bytes"] = c.execute(text(
                "SELECT pg_database_size(current_database())")).scalar()
            rows = c.execute(text(
                "SELECT c.relname AS tbl, "
                "pg_total_relation_size(c.oid) AS bytes, "
                "COALESCE(s.n_live_tup, 0) AS rows_est FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
                "WHERE n.nspname='public' AND c.relkind='r' "
                "ORDER BY bytes DESC LIMIT 15")).fetchall()
            out["tables"] = [{"table": r[0], "bytes": int(r[1] or 0),
                              "rows": int(r[2] or 0)} for r in rows]
        except Exception as exc:          # sqlite (tests) has no pg_*
            out["error"] = str(exc)[:200]
    return out


@app.get("/api/admin/mls/deployed-eval")
def mls_admin_deployed_eval(request: Request, sims: int = Query(3000)):
    """Operator-only: score the EXACT deployed probability generator
    (V9.3 eval F9). The public ladder scores an analytic
    independent-Poisson representation; production also samples red cards
    and calibrates. Monte Carlo and therefore slow — a diagnostic, never a
    boot step."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import model_eval
    return model_eval.evaluate_deployed(n_sims=max(500, min(sims, 10000)))


@app.get("/api/mls/slate")
def mls_slate(date: str | None = Query(None, pattern=r"^\d{8}$")):
    """The slate scorecard (V8.1 eval step 2): every fixture on a
    matchday classified into exactly one state (PASS / MISSED /
    CAPTURE_FAILED / LOCK_FAILED / EXECUTION_NOT_READY /
    SETTLEMENT_FAILED / LEGACY_UNSCORABLE / PENDING / INTEGRITY_FAILED),
    plus the operational-qualification invariants. Defaults to the
    soonest upcoming slate. Public read-only, 30s cache."""
    from src.mls import _cached
    try:
        from src.live import slate
        key = f"mls_slate:{date or 'next'}"
        return _cached(key, 30, lambda: slate.slate_report(date)) or {}
    except Exception as exc:
        print(f"[mls] slate failed: {exc}")
        raise HTTPException(503, "slate unavailable")


@app.get("/api/mls/audit")
def mls_audit():
    """The lock acceptance audit: every T-10 lock's integrity
    invariants, retained missed-locks and failed snapshots, and a
    content hash. Public read-only — it exposes only aggregate research
    integrity, and publishing it is the transparency the fail-closed
    lock design exists to demonstrate. 30s cache."""
    from src.mls import _cached
    try:
        from src.live import audit as live_audit
        out = _cached("mls_audit", 30, live_audit.lock_audit)
    except Exception as exc:
        print(f"[mls] audit failed: {exc}")
        raise HTTPException(503, "audit unavailable")
    return out or {"skipped": "dormant"}


@app.get("/api/mls/risk")
def mls_risk():
    """The risk engine's live state: the versioned policy, any active
    kill switches, and current open exposure. Public read-only. 15s
    cache."""
    from src.mls import _cached
    try:
        from src.live import risk
        return _cached("mls_risk", 15, risk.assess) or {}
    except Exception as exc:
        print(f"[mls] risk assess failed: {exc}")
        raise HTTPException(503, "risk unavailable")


@app.get("/api/mls/metrics")
def mls_metrics():
    """Operational metrics for observability (V8.1 eval Phase 10):
    fixture/quote freshness, lock success, missed locks, scheduler
    health, paper P&L. Public read-only, machine-readable. 15s cache."""
    from src.mls import _cached
    try:
        from src.live import observability
        return _cached("mls_metrics", 15, observability.metrics) or {}
    except Exception as exc:
        print(f"[mls] metrics failed: {exc}")
        raise HTTPException(503, "metrics unavailable")


@app.get("/api/hunter/findings")
def hunter_findings(competition: str | None = Query(None, max_length=64),
                    type: str | None = Query(None, max_length=32),
                    status: str | None = Query(None, max_length=16),
                    limit: int = Query(100, ge=1, le=500)):
    """Kalshi market-hunter findings — OBSERVATIONAL records only, with
    their denominators (cycles run, markets scanned, findings per type)
    and the last-cycle heartbeat so a dead scanner is distinguishable
    from a quiet market. Filters: competition (slug or series ticker),
    type (SUM_BELOW_ONE | CROSSED_BOOK | POST_CERTAINTY | WIDE_SPREAD |
    THIN_BOOK | IN_PLAY_OVERREACTION | MODEL_EDGE), status (open |
    expired). Public read-only; nothing here is advice and no order
    path exists."""
    try:
        from src.live import hunter
        return hunter.findings_report(competition=competition,
                                      finding_type=type, status=status,
                                      limit=limit)
    except Exception as exc:
        print(f"[hunter] findings failed: {exc}")
        raise HTTPException(503, "hunter findings unavailable")


@app.get("/api/hunter/live-coverage")
def hunter_live_coverage():
    """Which competitions API-Football was MEASURED to serve live in-play
    data for, per competition, with the date measured and the number of
    fixtures the verdict rests on.

    Exposed because an unexposed coverage verdict is a private assumption,
    and the in-play detector's whole claim is that coverage is measured
    rather than assumed. Statistics and EVENTS coverage are reported
    separately: events coverage is strictly broader (measured — some
    competitions serve goal events with zero statistic types), and one
    combined flag would hide that.

    Also carries the three conditioning states with their strength ranks,
    so the inference direction is readable from the API and not only from
    the source: `conditioning_observed` ranks LOWEST, because an explaining
    event makes a market's move more reasonable and therefore makes the
    finding WEAKER.

    Public read-only. Observational; nothing here is advice, and none of
    it may reach a model — live xG carries post-lock information."""
    try:
        from src.live import apifootball_live
        return apifootball_live.coverage_report()
    except Exception as exc:
        print(f"[hunter] live coverage failed: {exc}")
        raise HTTPException(503, "hunter live coverage unavailable")


@app.get("/api/mls/paper")
def mls_paper():
    """The paper-trading ledger P&L: signals, fills, rejections (with
    reasons), and settled economics. PAPER only — execution evidence
    against frozen T-10 books, never a real position. 30s cache."""
    from src.mls import _cached
    try:
        from src.live import paper
        return _cached("mls_paper", 30, paper.paper_summary) or {}
    except Exception as exc:
        print(f"[mls] paper summary failed: {exc}")
        raise HTTPException(503, "paper summary unavailable")


@app.get("/api/mls/model-eval")
def mls_model_eval():
    """The model-development ladder evaluation: M0/M1/M2 scored with
    analytic (noise-free) 3-way probabilities under rolling-origin
    validation, with match-cluster bootstrap CIs on each pairwise edge,
    plus the approval-decision record. Public read-only; 1h cache
    (rolling-origin + bootstrap is expensive)."""
    from src.mls import _cached

    def _run():
        from src.live import model_eval
        rep = model_eval.evaluate_ladder(n_boot=1000)
        rep["approval_record"] = model_eval.approval_record(rep)
        return rep
    try:
        return _cached("mls_model_eval", 3600, _run)
    except Exception as exc:
        print(f"[mls] model-eval failed: {exc}")
        raise HTTPException(503, "model-eval unavailable")


@app.get("/api/mls/approval")
def mls_approval():
    """The persisted model-approval DECISION the runtime is operating
    under (V9 eval F1/F10 — pre-slate evidence). Reads the STORED immutable
    row ONLY and never recomputes an evaluation; returns
    `approval_decision_missing` rather than inventing one. Public
    read-only. Not cached: the query is a single indexed row, and caching
    would risk serving a transient boot-time 'missing' after the decision
    lands."""
    try:
        from src.live import model_eval
        return model_eval.current_approval_decision()
    except Exception as exc:
        print(f"[mls] approval failed: {exc}")
        raise HTTPException(503, "approval unavailable")


@app.get("/api/mls/stats-coverage")
def mls_stats_coverage():
    """Official-stats coverage: how many completed matches we hold team
    stats (and provider xG) and player stats for. The verifiable answer to
    'do we have all the stats?'. Public read-only, 60s cache."""
    from src.mls import _cached

    def _run():
        from src.live import mls_stats, player_bridge
        cov = mls_stats.coverage()
        cov["player_id_bridge"] = player_bridge.bridge_coverage()
        return cov
    try:
        return _cached("mls_stats_coverage", 60, _run) or {}
    except Exception as exc:
        print(f"[mls] stats-coverage failed: {exc}")
        raise HTTPException(503, "stats-coverage unavailable")


@app.post("/api/admin/mls/stats-backfill")
def mls_stats_backfill(request: Request, players: bool = Query(True),
                       skip_existing: bool = Query(True)):
    """Operator-only: backfill the full season's official stats in the
    background (fills the player-history gap the team-only boot leaves).
    Additive — no model output changes. Poll /api/mls/stats-coverage for
    progress. The middleware enforces the token in read-only mode; the
    explicit check keeps it locked even if that mode is ever off."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import mls_stats
    return mls_stats.start_backfill(with_players=players,
                                    skip_existing=skip_existing)


@app.get("/api/mls/corpus")
def mls_corpus(version: str | None = Query(None), full: bool = Query(False),
               preview: bool = Query(False)):
    """The prospective research corpus. Published versions are IMMUTABLE
    and served from STORED bytes (V9 eval F3), never rebuilt from current
    state: pass ?version=... for a published manifest, add &full=1 for the
    whole self-contained bundle. With no version this lists the published
    versions plus the latest manifest. ?preview=1 builds the CURRENT
    (unpublished) state, explicitly labeled as a non-immutable preview.
    Public read-only — the downloadable evidence base."""
    try:
        from src.mls import _cached
        from src.live import corpus as live_corpus
        if preview:
            # V9.3 eval F20: the preview assembles the WHOLE current state
            # and measured 46s on production once the research sections
            # (F10) were added — far too long to hold a worker on a public
            # route. Cache it so the cost is paid at most once per window
            # no matter how many callers ask; published versions are served
            # from stored bytes and are unaffected.
            bundle = _cached("mls_corpus_preview", 300,
                             live_corpus.build_corpus)
            if full:
                # V9.3 eval F20: a public read must not return an unbounded
                # body. The PREVIEW is built from current state and grows
                # with the database, so it is size-capped; published
                # versions are served from stored bytes and stay available
                # for the real download path.
                import json as _j
                size = len(_j.dumps(bundle, default=str))
                if size > config.MAX_PUBLIC_BODY_BYTES:
                    raise HTTPException(
                        413, "corpus preview too large to serve inline "
                             f"({size} bytes > {config.MAX_PUBLIC_BODY_BYTES}); "
                             "publish a version and download that instead")
                return bundle
            man = bundle.get("manifest", bundle)
            if isinstance(man, dict):
                man = {**man, "published": False,
                       "note": ("UNPUBLISHED preview of current DB state — "
                                "NOT immutable; publish to freeze a "
                                "version")}
            return man
        if version:
            served = live_corpus.get_published(version, full=full)
            if served is None:
                raise HTTPException(404, "no such published corpus version")
            return served
        published = live_corpus.list_published()
        latest = (live_corpus.get_published(published[0]["version"])
                  if published else None)
        return {"published_versions": published, "latest": latest}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[mls] corpus failed: {exc}")
        raise HTTPException(503, "corpus unavailable")


@app.post("/api/admin/mls/corpus/publish")
def mls_admin_publish_corpus(request: Request, version: str = Query(...)):
    """Operator-only: freeze the CURRENT corpus as an immutable published
    version (V9 eval F3). Re-publishing an existing version is refused —
    bump the version. Read-only mode already enforces the token; the
    explicit check keeps this locked even if that mode is ever off."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import corpus as live_corpus
    return live_corpus.publish_corpus(version)


@app.post("/api/admin/mls/approval/activate")
def mls_admin_activate_approval(request: Request,
                                corpus_version: str | None = Query(None),
                                n_boot: int = Query(1000)):
    """Operator-only: EXPLICITLY evaluate and activate an approval.

    V9.5 eval H6. Boot used to create an approval whenever none matched
    the current engine — and because the engine signature includes
    `code_revision`, that meant every deploy silently minted a fresh
    approval from whatever the live database held, while the documented
    governance claim was that re-evaluation is an explicit operator
    action. Boot now loads and fails closed; this is the only path that
    creates one.

    Note what the decision records about itself: `evaluation_source` is
    `live_database`. The ladder reads current database state, NOT the
    published corpus bytes, so the corpus binding files the decision
    against a corpus rather than proving it was computed from one."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import corpus as live_corpus
    from src.live import model_eval
    target = corpus_version or live_corpus.latest_published_version()
    res = model_eval.ensure_approval_decision(
        corpus_version=target, n_boot=n_boot, force=True, allow_create=True)
    return {**res, "bound_corpus_version": target,
            "activated_by": "operator",
            "generated_at": utcnow().isoformat()}


@app.post("/api/admin/mls/approval/bind-corpus")
def mls_admin_bind_corpus(request: Request,
                          corpus_version: str | None = Query(None),
                          n_boot: int = Query(1000)):
    """Operator-only: persist the approval decision BOUND to a published
    corpus.

    Every decision to date recorded corpus_version=null — boot called
    ensure_approval_decision() with no corpus, so the evidence contract's
    "approved against THIS frozen corpus" link was never actually made.
    With a corpus published, this evaluates and persists a new immutable
    decision carrying that corpus version and its manifest hash inside
    the decision's content hash. Defaults to the newest published
    corpus, and refuses when none exists — binding to nothing is the
    condition it is here to fix."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src.live import corpus as live_corpus
    from src.live import model_eval
    target = corpus_version or live_corpus.latest_published_version()
    if not target:
        raise HTTPException(
            409, "no published corpus to bind to — publish one first "
                 "(POST /api/admin/mls/corpus/publish?version=...)")
    res = model_eval.ensure_approval_decision(corpus_version=target,
                                              n_boot=n_boot, force=True)
    return {**res, "bound_corpus_version": target,
            "generated_at": utcnow().isoformat()}


@app.get("/api/mls/replay/{run_id}")
def mls_replay(run_id: str):
    """Independent reproducibility check: replay a run from its stored
    input artifact ALONE and confirm it reproduces the stored
    probabilities. Public read-only — this IS the evidence behind the
    'independently model-reproducible' claim."""
    if not (run_id.replace("-", "").isalnum() and len(run_id) <= 36):
        raise HTTPException(404, "unknown run")
    try:
        from src.live import audit as live_audit
        return live_audit.verify_replay(run_id)
    except Exception as exc:
        print(f"[mls] replay failed for {run_id}: {exc}")
        raise HTTPException(503, "replay unavailable")


@app.get("/api/mls/odds")
def mls_odds():
    """The shadow odds board: every upcoming fixture's newest complete
    prediction run. Shadow-labeled; never a recommendation."""
    odds = []
    try:
        from src.live import runs as live_runs
        odds = live_runs.latest_odds()
    except Exception as exc:
        print(f"[mls] odds board failed: {exc}")
    return {"odds": odds, "shadow": True,
            "real_money_signals": config.REAL_MONEY_SIGNALS_ENABLED,
            "generated_at": utcnow().isoformat()}


@app.get("/api/ready")
def ready():
    """Readiness, distinct from liveness (V7 evaluation F7): reports
    whether the archival state a fresh container rebuilds at boot is
    actually present, with expected-vs-actual counts. /api/health stays a
    bare liveness probe; THIS is the endpoint that must gate "the archive
    is being served correctly". Expectations are pinned to the completed
    2026 configuration (84 settled positions, 6 canonical lock bundles)."""
    from sqlalchemy import func as _func

    from src import archive
    from src.db import BotPosition, MatchResult

    now = utcnow()
    with SessionLocal() as s:
        results = s.execute(select(_func.count())
                            .select_from(MatchResult)).scalar_one()
        ledger = s.execute(select(_func.count())
                           .select_from(BotPosition)).scalar_one()
    expected_results = sum(1 for m in load_schedule()
                           if m.fully_resolved and m.kickoff < now)
    bundles = len(archive.available_lock_bundles())
    from src.live import db as live_db
    live = live_db.status()
    if live.get("connected"):
        try:
            from src.live import runs as live_runs
            live["shadow"] = live_runs.shadow_counts()
        except Exception as exc:
            live["shadow"] = {"error": str(exc)[:200]}
    archive_ok = (results >= expected_results and ledger == 84
                  and bundles == 6)
    live_ok = (not live["enabled"]) or (
        live.get("connected") and live.get("migrations_current")
        and live.get("competition_seeded"))
    # Mode-specific readiness (V9 eval F17): a single boolean hid whether
    # the mode actually being served is ready. shadow collection requires
    # the live plane AND the shadow pipeline's own blockers to clear
    # (approved model, valid approval decision, mapped upcoming markets…);
    # paper execution additionally requires the paper switch on and no
    # trading kill switch. Top-level `ready` reflects the served mode.
    shadow = live.get("shadow") or {}
    mode = "mls_shadow" if config.MLS_SHADOW_ENABLED else "archive"
    shadow_collection_ready = bool(
        config.MLS_SHADOW_ENABLED and live.get("connected")
        and live.get("migrations_current") and live.get("competition_seeded")
        and shadow.get("shadow_ready"))
    # the paper ENGINE can be operational while NEW ENTRIES are halted by a
    # data-driven kill switch (V9.1 eval F7): settlement/reconciliation must
    # continue even when the daily-loss limit or a stale-data switch trips,
    # so the two states are distinct and new-entry readiness incorporates
    # active_kill_switches — which paper readiness previously ignored.
    paper_engine_operational = bool(
        shadow_collection_ready and config.PAPER_TRADING_ENABLED
        and not config.GLOBAL_TRADING_DISABLED
        and not config.COMPETITION_TRADING_DISABLED)
    paper_kill_switches: list = []
    if config.MLS_SHADOW_ENABLED and live.get("connected"):
        try:
            from src.live import db as _live_db
            from src.live import risk
            _s = _live_db.get_session()
            try:
                paper_kill_switches = risk.active_kill_switches(_s)
            finally:
                _s.close()
        except Exception as exc:
            paper_kill_switches = [f"error:{str(exc)[:40]}"]
    paper_execution_ready = bool(paper_engine_operational
                                 and not paper_kill_switches)
    readiness = {
        "archive_ready": bool(archive_ok),
        "shadow_collection_ready": shadow_collection_ready,
        "paper_engine_operational": paper_engine_operational,
        "paper_new_entries_allowed": paper_execution_ready,
        "paper_execution_ready": paper_execution_ready,      # back-compat
        "paper_kill_switches": paper_kill_switches,
    }
    top_ready = (archive_ok and live_ok
                 and (shadow_collection_ready if mode == "mls_shadow"
                      else True))
    return {"ready": bool(top_ready),
            "mode": mode,
            "readiness": readiness,
            "shadow_blockers": shadow.get("blockers", []),
            "results": results, "expected_results": expected_results,
            "ledger_positions": ledger, "expected_ledger": 84,
            "lock_bundles": bundles, "expected_lock_bundles": 6,
            "live": live,
            "real_money_signals": config.REAL_MONEY_SIGNALS_ENABLED,
            "time": now.isoformat()}


@app.get("/api/matches/upcoming")
def upcoming_matches(hours_ahead: int = Query(48, ge=1, le=720)):
    now = utcnow()
    horizon = now + timedelta(hours=hours_ahead)
    out = []
    for m in load_schedule():
        if not (now < m.kickoff <= horizon):
            continue
        cached = latest_for_match(m.match_id)
        # A QF slot whose feeder hasn't finished yet is a placeholder ("USA/BEL
        # winner"); the UI shows it as TBD and skips the prediction link. A
        # resolved team with no sourced TEAM_STATS runs on _DEFAULT — flagged
        # provisional so the model's humility is visible, never hidden.
        tbd = not m.fully_resolved
        prov = [t for t in (m.home, m.away)
                if (t == m.home and m.home_resolved or
                    t == m.away and m.away_resolved)
                and not has_sourced_stats(t)]
        out.append({
            "match_id": m.match_id,
            "home": m.home,
            "away": m.away,
            "group": m.group,
            "stage": m.stage,
            "venue": m.venue,
            "kickoff": m.kickoff.isoformat(),
            "seconds_to_kickoff": int((m.kickoff - now).total_seconds()),
            "has_prediction": cached is not None,
            "is_final": bool(cached and cached["is_final"]),
            "confidence": cached["confidence"] if cached else None,
            "tbd": tbd,
            "home_resolved": m.home_resolved,
            "away_resolved": m.away_resolved,
            "provisional_stats": prov,
        })
    out.sort(key=lambda x: x["seconds_to_kickoff"])
    return {"matches": out, "generated_at": now.isoformat()}


@app.get("/api/suggestions")
def suggestions(limit: int = Query(50, ge=1, le=200)):
    """Ranking board: every market on every trackable match, filtered by
    LIKELIHOOD only — edge is displayed, never a gate — sorted most-likely
    first with a deterministic tiebreak (likelihood ↓, edge ↓, kickoff ↑).

    Tier 1 keeps markets at/above SUGGEST_PRIMARY_FLOOR (49%). If nothing
    across ALL matches clears it, tier 2 falls back to SUGGEST_FALLBACK_FLOOR
    (40%). If even that is empty, the board is honestly empty: tier_used is
    null so the frontend can say so instead of pretending. No per-match cap —
    one match may contribute many rows. TAKE/alert logic stays edge-based
    elsewhere; this endpoint is purely the likelihood board.
    """
    now = utcnow()
    pool: list[dict] = []
    for m in load_schedule():
        if not is_trackable(m, now, config.HOURLY_PREDICTION_WINDOW_HOURS,
                            config.TRACK_HOURS_AFTER_KICKOFF):
            continue
        # Drop a match's bets the INSTANT it ends (a frozen result exists),
        # not 4h later — separate from the scoreboard's FT grace window.
        if live_state_svc.is_finished(m.match_id):
            continue
        snap = latest_for_match(m.match_id)
        if not snap:
            continue
        for mkt in snap["markets"]:
            pool.append({
                "match_id": m.match_id,
                "home": m.home,
                "away": m.away,
                "market_id": mkt["market_id"],
                "market_title": mkt["market_title"],
                "outcome_key": mkt.get("outcome_key"),
                "kickoff": m.kickoff.isoformat(),
                "kalshi_odds": mkt["kalshi_odds"],
                "model_probability": mkt["model_probability"],
                "implied_probability": mkt["implied_probability"],
                "edge": mkt["edge"],
                "expected_value": mkt["expected_value"],
                "confidence": snap["confidence"],
                "is_final": snap["is_final"],
            })

    tier_used = None
    floor = config.SUGGEST_PRIMARY_FLOOR
    board = [s for s in pool if s["model_probability"] >= floor]
    if board:
        tier_used = int(round(floor * 100))
    else:
        floor = config.SUGGEST_FALLBACK_FLOOR
        board = [s for s in pool if s["model_probability"] >= floor]
        if board:
            tier_used = int(round(floor * 100))

    board.sort(key=lambda s: (-s["model_probability"], -s["edge"], s["kickoff"]))
    return {"suggestions": board[:limit], "tier_used": tier_used,
            "generated_at": now.isoformat()}


@app.get("/api/prediction/{match_id}")
def get_prediction(match_id: str, force_refresh: bool = False,
                   request: Request = None):
    match = get_match(match_id)
    if not match:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")

    # A finished match's page is a REVIEW page: never re-simulate (fresh
    # runs see only settled books and would blank the table). Serve the
    # T-10 LOCKED batch — the model's committed pre-kickoff numbers, kept
    # for exactly this "was my model any good?" check — else the last
    # cached batch. force_refresh is deliberately ignored here: a tapped
    # Refresh button must never wipe the review view.
    if live_state_svc.is_finished(match_id):
        locked = latest_for_match(match_id, final_only=True)
        if locked and locked["markets"]:
            return {"freshness": "locked", **locked, "is_stale": False}
        # The committed lock bundle is the canonical copy — the DB rows
        # die on deploy wipes (V7 evaluation F1). Serve it verbatim.
        from src import archive
        archived = archive.review_payload(match_id)
        if archived:
            return {"freshness": "locked", **archived}
        cached = latest_for_match(match_id)
        if cached and cached["markets"]:
            return {"freshness": "cached", **cached, "is_stale": False}
        # No frozen record survives and none was archived. The old
        # fallback re-simulated with the CURRENT model and stats and
        # rendered it on the review page — a provenance failure (V7
        # evaluation F1). An archive says "missing"; it never invents.
        return {"freshness": "archive-incomplete", "match_id": match_id,
                "generated_at": utcnow().isoformat(), "age_seconds": 0,
                "is_stale": False, "source": "archive_incomplete",
                "is_final": False, "xg": None, "scorelines": [],
                "summary": None, "confidence": None, "markets": [],
                "suggestions": [],
                "archive_note": ("no frozen pre-match record survives for "
                                 "this match — its T-10 lock predates the "
                                 "archive discipline; retrospective "
                                 "re-simulation is deliberately not shown "
                                 "on review pages")}

    public = config.PUBLIC_READ_ONLY and (request is None
                                          or not _admin_ok(request))
    if force_refresh and public:
        # explicit refusal, not a silent downgrade: the frontend must
        # never report "fresh simulation done" for a request the server
        # refused (V7 evaluation §7.3)
        raise HTTPException(403, "read-only mode: fresh computation "
                                 "requires operator credentials")
    if not force_refresh:
        cached = latest_for_match(match_id)
        if cached and not cached["is_stale"]:
            return {"freshness": "cached", **cached}
        if public:
            # computing would PERSIST prediction rows — a mutating GET in
            # effect (V7 evaluation F2). The anonymous public gets the
            # stale copy, honestly labeled, or an honest empty. Never a
            # write.
            if cached:
                return {"freshness": "stale-archive", **cached}
            return {"freshness": "unavailable", "match_id": match_id,
                    "generated_at": utcnow().isoformat(), "age_seconds": 0,
                    "is_stale": True, "source": "unavailable",
                    "is_final": False, "xg": None, "scorelines": [],
                    "summary": None, "confidence": None, "markets": [],
                    "suggestions": []}

    t0 = time.time()
    result = engine.run_for_match(match, source="on_demand")
    refresh_model_cache(result)   # keep the ripeness poller's edge current
    fresh = latest_for_match(match_id)
    if fresh is None:
        # Zero priceable Kalshi markets (e.g. a bracket slot still carrying
        # a placeholder side) persists zero Prediction rows — serve the
        # simulation honestly with an empty markets list, never a 500.
        sim = result["simulation"]
        fresh = {
            "match_id": match_id,
            "generated_at": result["generated_at"],
            "age_seconds": 0,
            "is_stale": False,
            "source": result["source"],
            "is_final": result["is_final"],
            "xg": sim["xg"],
            "scorelines": sim["scorelines"],
            "summary": {"full_time": sim["outcomes"],
                        "advance": sim.get("advance"),
                        "halves": sim.get("halves")},
            "confidence": sim["confidence"],
            "markets": [],
        }
    return {
        "freshness": "fresh",
        "inference_time_ms": round((time.time() - t0) * 1000),
        "suggestions": result["suggestions"],
        **fresh,
    }


class LiveStateIn(BaseModel):
    current_home: int = 0
    current_away: int = 0
    minutes_elapsed: float = 0.0
    # red cards as COUNTS (0-3 per side); legacy booleans coerce (True -> 1)
    red_home: int = 0
    red_away: int = 0
    # match segment: auto | regulation | et | pens. "auto" infers from the
    # minute (>90 in a knockout = extra time).
    phase: str = "auto"
    # user-set attack levers for qualitative reads (1.0 = no adjustment)
    attack_home_mult: float = 1.0
    attack_away_mult: float = 1.0


@app.post("/api/prediction/{match_id}/live")
def live_prediction(match_id: str, state: LiveStateIn):
    """Layer 3: price current markets against a manually-entered live state.
    Ephemeral (not persisted), edge-ungated, honestly framed — see
    SuggesterEngine.price_live()."""
    match = get_match(match_id)
    if not match:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    if state.current_home < 0 or state.current_away < 0:
        raise HTTPException(422, "score cannot be negative")
    if not (0 <= state.minutes_elapsed <= 130):
        raise HTTPException(422, "minutes_elapsed out of range")
    if state.phase not in ("auto", "regulation", "et", "pens"):
        raise HTTPException(422, "phase must be auto|regulation|et|pens")
    if state.phase in ("et", "pens") and match.stage != "knockout":
        raise HTTPException(422, "extra time/penalties only exist in knockouts")
    for r in (state.red_home, state.red_away):
        if not (0 <= r <= 3):
            raise HTTPException(422, "red cards out of range (0-3)")
    for m in (state.attack_home_mult, state.attack_away_mult):
        if not (0.25 <= m <= 3.0):
            raise HTTPException(422, "attack lever out of range (0.25-3.0)")
    try:
        return engine.price_live(
            match, state.current_home, state.current_away,
            state.minutes_elapsed, state.red_home, state.red_away,
            state.attack_home_mult, state.attack_away_mult,
            phase=state.phase)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/prediction/{match_id}/live-state")
def fetch_live_state(match_id: str):
    """Layer 2: auto-fetch the real current state (score/minute/red cards)
    for a match from API-Football, so the live panel can pre-fill instead of
    the user typing it. Returns {available: false, ...} (never an error) when
    the feed is unconfigured, over budget, or the match isn't live — the UI
    then falls back to manual entry."""
    match = get_match(match_id)
    if not match:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    state = live_state_for(match.home, match.away)
    if state is None:
        return {"available": False, "match_id": match_id,
                "budget": budget_status(),
                "reason": ("feed not configured" if not config.API_FOOTBALL_KEY
                           else "no live match found or feed unavailable")}
    from src.live_auto import sim_minutes
    return {
        "available": True,
        "match_id": match_id,
        "current_home": state["home_goals"],
        "current_away": state["away_goals"],
        # match PROGRESS, not the wall clock: 1H stoppage clamps to 45'
        # so a manual simulation doesn't eat the second half's budget
        "minutes_elapsed": sim_minutes(
            float(state["minutes_elapsed"] or 0.0),
            state.get("status_short") or ""),
        "red_home": state["red_home"],
        "red_away": state["red_away"],
        "status_short": state["status_short"],
        "is_live": state["is_live"],
        "is_finished": state["is_finished"],
        "goals_list": state.get("goals_list", []),
        "budget": budget_status(),
    }


@app.get("/api/live-scores")
def live_scores():
    """Live scoreboard for the landing page. Served from the live-state
    snapshot store (refreshed every poll), NOT directly from the feed — so a
    match in a between-periods break (90'->ET, ET->penalties) doesn't vanish,
    and just-finished matches show as FT cards for a grace window. Costs no
    feed call itself (the poller does the fetching)."""
    return {"live": live_state_svc.scoreboard_entries(),
            "budget": budget_status(),
            "generated_at": utcnow().isoformat()}


@app.get("/api/past-matches")
def past_matches():
    """Finished matches, most-recent first, for the Past matches section."""
    return {"past": live_state_svc.past_matches(),
            "generated_at": utcnow().isoformat()}


@app.get("/api/team-info/{match_id}")
def team_info(match_id: str):
    """Both teams' scouting blurbs + headline stats for the match page's
    "How they play" cards. A READ AID for the bettor — these blurbs never
    touch probabilities. Team names resolved from the schedule; a placeholder
    QF slot returns empty blurbs until the bracket fills in."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")

    def blurb(team: str, resolved: bool) -> dict:
        if not resolved:
            return {"team": team, "scouting": "", "resolved": False,
                    "provisional": False}
        s = get_team_stats(team)
        return {
            "team": team,
            "scouting": s.get("scouting", ""),
            "resolved": True,
            "provisional": not has_sourced_stats(team),
            "attack": s["attack"], "defence": s["defence"],
            "form": s["form"], "fatigue": s["fatigue"],
        }

    return {
        "match_id": match_id,
        "home": blurb(m.home, m.home_resolved),
        "away": blurb(m.away, m.away_resolved),
    }


@app.get("/api/prediction/{match_id}/live-auto")
def live_auto_stream(match_id: str):
    """The self-running live read: snapshot state + live shot stats ->
    derived attack levers -> rest-of-match simulation -> every open market
    priced. Server-cached ~25s so any number of viewers costs one cycle.
    Informational only, never a TAKE signal — the market knows the score."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    from src.live_auto import live_auto as _run
    cached = latest_for_match(match_id)
    return {"match_id": match_id,
            **_run(m, engine, (cached or {}).get("xg"))}


@app.get("/api/live-stats/{match_id}")
def live_match_stats(match_id: str):
    """Broadcast-style team stat rows (possession, shots, corners...) for a
    live or just-finished match, from ESPN's keyless boxscore. Cached 30s
    server-side; no feed budget."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    from src.live_feed import espn_match_stats
    return {"match_id": match_id, "home_team": m.home, "away_team": m.away,
            **espn_match_stats(m.home, m.away)}


@app.get("/api/team-news/{match_id}")
def team_news(match_id: str):
    """Matchday lineups (FACTS: starters / bench), from ESPN's keyless
    summary — typically posted ~1h before kickoff. Never a model input
    beyond settled-fact effects (an out-of-squad player can't score)."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    from src.live_feed import espn_lineups
    lu = espn_lineups(m.home, m.away)
    return {"match_id": match_id, "home_team": m.home, "away_team": m.away,
            "kickoff": m.kickoff.isoformat(), "venue": m.venue, **lu}


@app.get("/api/research/{match_id}")
def research_bundle(match_id: str):
    """The research record for one match, three aligned views per market:
    the T-10 LOCKED model numbers, the market's CLOSING/settlement state,
    and the frozen result. Closing rows exist once the post-FT snapshot
    has been captured (automatic at freeze; POST .../snapshot to backfill)."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    import json as _json

    from sqlalchemy import select as _select

    from src.db import MatchResult, OddsReading, Prediction
    from src.research import closing_rows

    with SessionLocal() as s:
        res = s.get(MatchResult, match_id)
        result = None if res is None else {
            "home_goals": res.home_goals, "away_goals": res.away_goals,
            "status_short": res.status_short,
            "finished_at": res.finished_at.isoformat() if res.finished_at else None,
            "goals": _json.loads(res.goals_json or "[]"),
        }
        # T-10 lock: newest is_final row per market
        locked = s.execute(
            _select(Prediction)
            .where(Prediction.match_id == match_id, Prediction.is_final)
            .order_by(Prediction.created_at.desc())
        ).scalars().all()
        seen: set[str] = set()
        final_lock = []
        for r in locked:
            if r.market_id in seen:
                continue
            seen.add(r.market_id)
            final_lock.append({
                "market_id": r.market_id, "market_title": r.market_title,
                "outcome_key": r.outcome_key,
                "model_probability": r.model_probability,
                "kalshi_odds": r.kalshi_odds,
                "implied_probability": r.implied_probability,
                "edge": r.edge, "confidence": r.confidence,
                "locked_at": r.created_at.isoformat() if r.created_at else None,
            })
        # DB rows die on deploy wipes; the committed bundle is the
        # canonical copy (V7 evaluation F1). Serve it verbatim.
        final_lock_source = "database"
        if not final_lock:
            from src import archive
            final_lock = archive.lock_rows(match_id)
            final_lock_source = ("canonical_archive" if final_lock
                                 else "absent")
        # last traded reading per market (the true pre-settlement close)
        reads = s.execute(
            _select(OddsReading)
            .where(OddsReading.match_id == match_id)
            .order_by(OddsReading.created_at.desc())
        ).scalars().all()
        rseen: set[str] = set()
        last_readings = []
        for r in reads:
            if r.market_id in rseen:
                continue
            rseen.add(r.market_id)
            last_readings.append({
                "market_id": r.market_id, "yes_price": r.yes_price,
                "model_probability": r.model_probability, "edge": r.edge,
                "read_at": r.created_at.isoformat() if r.created_at else None,
            })
    return {"match_id": match_id, "home_team": m.home, "away_team": m.away,
            "result": result, "final_lock": final_lock, "final_lock_source": final_lock_source,
            "closing": closing_rows(match_id),
            "last_readings": last_readings,
            "generated_at": utcnow().isoformat()}


@app.post("/api/research/{match_id}/snapshot")
def research_capture(match_id: str):
    """Capture (or backfill) the closing-market snapshot for a match.
    Idempotent — a match already snapshotted reports 'exists'. Works after
    settlement too: Kalshi keeps settled markets queryable by event."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    from src.research import capture_closing_snapshot
    return {"match_id": match_id, **capture_closing_snapshot(m)}


@app.get("/api/reference-odds/{match_id}")
def get_reference_odds(match_id: str):
    """Sportsbook reference odds (API-Football, display-only). Fills the
    gap while Kalshi hasn't listed a family yet (e.g. Correct Score opens
    1-2 days out). NEVER feeds the board, the strategy engine, or any
    edge gate — see src/reference_odds.py for the ground rules."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    from src.reference_odds import reference_odds
    return reference_odds(m, latest_for_match(match_id))


@app.get("/api/player-props/{match_id}")
def player_props(match_id: str):
    """Per-player anytime / first-goalscorer probabilities for a match —
    Poisson thinning of the match sim's team xG by each player's FIFA-PDF
    scoring share (see src/player_props.py for the math + honest limits).
    Model estimates only: Kalshi's player markets stay unpriced until their
    settlement rules are verified."""
    m = get_match(match_id)
    if not m:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    if not m.fully_resolved:
        return {"available": False, "match_id": match_id,
                "reason": "bracket not resolved"}
    snap = latest_for_match(match_id)
    if snap:
        xgh, xga = snap["xg"]["home"], snap["xg"]["away"]
    else:  # no cached sim yet — derive from the same xG model directly
        from src.models.xg_model import predict_xg
        xgh, xga = predict_xg(get_team_stats(m.home), get_team_stats(m.away))
    from src.player_props import props_for, join_markets, join_match_markets
    props = props_for(m.home, m.away, m.stage, xgh, xga)
    join_markets(m.home, props["home"])     # tournament-anytime + Kalshi rows
    join_markets(m.away, props["away"])
    join_match_markets(m.home, m.away, props)   # per-match 1+/2+/3+ + assists
    from src.live_feed import espn_lineups
    from src.player_props import apply_lineups
    apply_lineups(props, espn_lineups(m.home, m.away))  # facts-only squad status
    return {
        "available": True,
        "match_id": match_id,
        "home_team": m.home, "away_team": m.away,
        "stage": m.stage,
        **props,
        "generated_at": utcnow().isoformat(),
        "disclaimer": ("Model estimates from 5-match FIFA data. Minutes and "
                       "line-ups are not modelled; a substitute's share "
                       "reflects his tournament so far. Kalshi player "
                       "markets are not priced against these numbers."),
    }


@app.get("/api/bracket")
def bracket():
    """Current knockout bracket: which QF sides are known vs still placeholders,
    plus the list of resolved teams running on provisional (unsourced) stats.
    Read-only — the resolver job does the feed work on its own schedule."""
    status = bracket_status()
    status["provisional_teams"] = provisional_teams()
    status["generated_at"] = utcnow().isoformat()
    return status


@app.get("/api/live-feed/budget")
def live_feed_budget():
    """How many API-Football calls remain today (transparency + debugging)."""
    return budget_status()


@app.get("/api/spike-detector/state")
def spike_detector_state():
    """LOG-ONLY Layer 1: the scoreline the detector currently infers per
    trackable match, from Kalshi's score markets. Read-only, drives nothing —
    it's here so the detector can be eyeballed live while its thresholds are
    still being tuned."""
    now = utcnow()
    out = []
    for m in load_schedule():
        if not is_trackable(m, now, config.HOURLY_PREDICTION_WINDOW_HOURS,
                            config.TRACK_HOURS_AFTER_KICKOFF):
            continue
        leader = spike_detector.current_leader(m.match_id)
        out.append({
            "match_id": m.match_id,
            "inferred_score": f"{leader[0]}-{leader[1]}" if leader else None,
        })
    return {"matches": out, "note": "log-only; does not affect predictions"}


@app.get("/api/prediction/{match_id}/timeline")
def prediction_timeline(match_id: str, outcome_key: str = "home_win"):
    if not get_match(match_id):
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    points = timeline_for_match(match_id, outcome_key=outcome_key)
    return {"match_id": match_id, "outcome_key": outcome_key,
            "points": points, "count": len(points)}


@app.post("/api/prediction/{match_id}/refresh")
def refresh_prediction(match_id: str):
    match = get_match(match_id)
    if not match:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    result = engine.run_for_match(match, source="on_demand")
    refresh_model_cache(result)   # keep the ripeness poller's edge current
    return {"status": "refreshed", "match_id": match_id,
            "suggestions": result["suggestions"],
            "generated_at": result["generated_at"]}


@app.post("/api/refresh-all")
def refresh_all():
    """Force a fresh simulation + live Kalshi prices for every trackable
    match. One failing match never blocks the rest: it lands in `failed`
    and the loop continues, so the response always says exactly which
    matches are current and which are showing last-known data."""
    now = utcnow()
    t0 = time.time()
    refreshed: list[str] = []
    failed: list[str] = []
    for m in load_schedule():
        if not is_trackable(m, now, config.HOURLY_PREDICTION_WINDOW_HOURS,
                            config.TRACK_HOURS_AFTER_KICKOFF):
            continue
        try:
            result = engine.run_for_match(m, source="on_demand")
            refresh_model_cache(result)
            refreshed.append(m.match_id)
        except Exception as exc:          # isolate, report, move on
            print(f"[refresh-all] {m.match_id} FAILED: {exc}")
            failed.append(m.match_id)
    return {"refreshed": refreshed, "failed": failed,
            "duration_ms": round((time.time() - t0) * 1000),
            "generated_at": utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Watchlist + ripeness timing
# ---------------------------------------------------------------------------
from src.db import TimingAlert, WatchlistItem
from src.timing import compute_timing


class WatchIn(BaseModel):
    match_id: str
    market_id: str
    market_title: str | None = None


@app.get("/api/watchlist")
def get_watchlist():
    """Watched markets, each with its live ripeness score."""
    with SessionLocal() as session:
        items = session.execute(select(WatchlistItem)).scalars().all()
    out = []
    for item in items:
        match = get_match(item.match_id)
        timing = (compute_timing(item.market_id, match.kickoff)
                  if match else {"score": 0, "status": "match_over",
                                 "readings": 0, "components": {}, "reasons": []})
        out.append({
            "match_id": item.match_id,
            "market_id": item.market_id,
            "market_title": item.market_title,
            "watched_since": item.created_at.isoformat(),
            "timing": timing,
        })
    out.sort(key=lambda x: x["timing"]["score"], reverse=True)
    return {"watchlist": out, "alert_threshold": config.RIPENESS_ALERT_THRESHOLD}


@app.post("/api/watchlist")
def add_watch(body: WatchIn):
    if not get_match(body.match_id):
        raise HTTPException(404, f"Unknown match_id '{body.match_id}'")
    with SessionLocal() as session:
        exists = session.execute(
            select(WatchlistItem).where(WatchlistItem.market_id == body.market_id)
        ).scalar_one_or_none()
        if exists:
            return {"status": "already_watching", "market_id": body.market_id}
        session.add(WatchlistItem(match_id=body.match_id, market_id=body.market_id,
                                  market_title=body.market_title))
        session.commit()
    return {"status": "watching", "market_id": body.market_id,
            "note": f"You'll be alerted when the ripeness score crosses "
                    f"{config.RIPENESS_ALERT_THRESHOLD:.0f} with positive edge."}


@app.delete("/api/watchlist/{market_id}")
def remove_watch(market_id: str):
    with SessionLocal() as session:
        item = session.execute(
            select(WatchlistItem).where(WatchlistItem.market_id == market_id)
        ).scalar_one_or_none()
        if not item:
            raise HTTPException(404, "Not on watchlist")
        session.delete(item)
        session.commit()
    return {"status": "removed", "market_id": market_id}


@app.get("/api/timing/{match_id}/{market_id}")
def get_timing(match_id: str, market_id: str):
    """Full ripeness breakdown for any market (watched or not)."""
    match = get_match(match_id)
    if not match:
        raise HTTPException(404, f"Unknown match_id '{match_id}'")
    return compute_timing(market_id, match.kickoff)


@app.get("/api/bots")
def bots_ledger():
    """The strategy-lab: five paper bots, their bankrolls and ledgers.
    Hypothetical money betting real books — a laboratory for which betting
    philosophy actually pays, scored with the same fee model as the
    strategy page."""
    from sqlalchemy import func
    from src.bots import PERSONAS, START_BANKROLL, bankroll
    from src.db import BotPosition, OddsReading
    out = []
    with SessionLocal() as session:
        # newest odds reading per market holding an open position — the 30s
        # poll keeps these fresh near matches; a market with no reading
        # falls back to cost, so equity never invents a price
        open_mids = set(session.execute(
            select(BotPosition.market_id)
            .where(BotPosition.closed_at.is_(None))).scalars())
        marks: dict[str, float] = {}
        if open_mids:
            latest_ids = (select(func.max(OddsReading.id))
                          .where(OddsReading.market_id.in_(open_mids))
                          .group_by(OddsReading.market_id))
            for rd in session.execute(
                    select(OddsReading)
                    .where(OddsReading.id.in_(latest_ids))).scalars():
                # conservative mark: the BID (what an exit realizes);
                # ask fallback for legacy rows, documented as optimistic
                mark = rd.yes_bid if rd.yes_bid is not None else rd.yes_price
                if mark is not None:
                    marks[rd.market_id] = float(mark)
        for bot, persona in PERSONAS.items():
            rows = session.execute(
                select(BotPosition).where(BotPosition.bot == bot)
                .order_by(BotPosition.opened_at.desc())
            ).scalars().all()
            open_pos, closed_pos = [], []
            for r in rows:
                item = {
                    "match_id": r.match_id, "market_id": r.market_id,
                    "market_title": r.market_title,
                    "entry_price": r.entry_price, "contracts": r.contracts,
                    "cost": r.cost, "note": r.note,
                    "opened_at": r.opened_at.isoformat() if r.opened_at else None,
                }
                if r.closed_at is None:
                    mark = marks.get(r.market_id)
                    item["mark_price"] = mark
                    item["market_value"] = (round(r.contracts * mark, 2)
                                            if mark is not None else r.cost)
                    open_pos.append(item)
                else:
                    item.update({
                        "closed_at": r.closed_at.isoformat(),
                        "close_price": r.close_price,
                        "close_reason": r.close_reason,
                        "net": round((r.pnl or 0.0) - r.cost, 2),
                    })
                    closed_pos.append(item)
            wins = sum(1 for c in closed_pos if c["net"] > 0)
            cash = bankroll(bot, session)
            # mark-to-market: open positions at the newest polled price
            # (fee-free mark; realized fees still hit on exit/settlement)
            equity = cash + sum(p["market_value"] for p in open_pos)
            out.append({
                "bot": bot, **persona,
                "bankroll": cash,
                "equity": round(equity, 2),
                "net_pnl": round(equity - START_BANKROLL, 2),
                "open": open_pos,
                "closed": closed_pos[:20],
                "trades": len(closed_pos),
                "wins": wins,
            })
    return {"start_bankroll": START_BANKROLL, "bots": out,
            "generated_at": utcnow().isoformat()}


@app.post("/api/alerts/test")
def alerts_test():
    """Fire a test message through every configured alert channel and
    report PER-LEG delivery, so a silent failure (bad webhook, mistyped
    ntfy topic, whitespace in an env var) is visible in the response
    instead of only in server logs. Each leg is exercised directly —
    the fan-out copy-to-detail is the gate's concern, not this probe's.

    The probe body lives in `src/alerts.py` because the transports are
    PRIVATE to that module: this route asks the gate module to test
    itself rather than reaching past it for a raw sender."""
    from src import alerts
    return alerts.channel_probe()


@app.get("/api/admin/alerts/refusals")
def alerts_refusals(request: Request, limit: int = Query(50, ge=1, le=200)):
    """Operator-only, READ-ONLY: what the alert gate has REFUSED since
    this process started, and how many of each class.

    A gate that refuses silently is indistinguishable from a transport
    that is down, and "nothing arrived" is exactly the shape of the two
    incidents this project has already had (DiskFull behind
    {"created": 0}; the VARCHAR truncation that erased every fill). The
    ledger carries the call site and the reason — never the refused
    message, which would put the withheld betting content back on a
    readable surface."""
    if not _admin_ok(request):
        raise HTTPException(403, "operator credentials required")
    from src import alerts
    return {"refusals": alerts.recent_refusals(limit),
            "counts": alerts.refusal_counts(),
            "real_money_signals_enabled":
                config.REAL_MONEY_SIGNALS_ENABLED,
            "note": ("process-local ring buffer; the printed log line is "
                     "the durable record"),
            "generated_at": utcnow().isoformat()}


@app.get("/api/positions")
def positions_list():
    """Son's real tracked positions with live HOLD/EXIT verdicts. In play,
    prices come from the live_auto cycle; pre-match, from the latest
    prediction batch. Read-only: never fires alerts."""
    from src.cache import latest_for_match
    from src.db import MatchLiveSnapshot, TrackedPosition
    from src.live_auto import live_auto
    from src.positions import evaluate_positions
    from src.schedule_data import load_schedule
    out = []
    with SessionLocal() as session:
        match_ids = set(session.execute(
            select(TrackedPosition.match_id)
            .where(TrackedPosition.closed_at.is_(None))).scalars())
        live_ids = set(session.execute(
            select(MatchLiveSnapshot.match_id)).scalars())
    for m in load_schedule():
        if m.match_id not in match_ids:
            continue
        rows = {}
        minute = None
        if m.match_id in live_ids:
            try:
                la = live_auto(m, engine,
                               (latest_for_match(m.match_id) or {}).get("xg"))
                if la.get("available"):
                    rows = {r["market_id"]: r for r in la.get("markets", [])}
                    minute = (la.get("live_state") or {}).get("minutes_elapsed")
            except Exception:
                rows = {}
        if not rows:
            batch = latest_for_match(m.match_id) or {}
            rows = {r["market_id"]: r for r in batch.get("markets", [])}
        out.extend(evaluate_positions(rows, m.match_id, minute))
    return {"positions": out, "generated_at": utcnow().isoformat()}


@app.post("/api/positions")
def positions_add(payload: dict):
    """Record real positions: {"positions": [{match_id, market_id,
    market_title?, entry_price, contracts, cost?, note?}]}. cost defaults
    to contracts*entry_price + the modelled fee."""
    from src.db import TrackedPosition
    added = []
    with SessionLocal() as session:
        for p in payload.get("positions") or []:
            if not p.get("market_id") or not p.get("match_id"):
                continue
            ep = float(p["entry_price"]); n = int(p["contracts"])
            cost = p.get("cost")
            if cost is None:
                cost = round(n * (ep + 0.07 * ep * (1 - ep)), 2)
            pos = TrackedPosition(
                match_id=p["match_id"], market_id=p["market_id"],
                market_title=p.get("market_title") or p["market_id"],
                entry_price=ep, contracts=n, cost=float(cost),
                note=p.get("note") or "")
            session.add(pos)
            session.flush()
            added.append(pos.id)
        session.commit()
    return {"added": len(added), "ids": added}


@app.delete("/api/positions/{pos_id}")
def positions_close(pos_id: int, note: str = Query("closed by user")):
    """Mark a tracked position closed (you exited / it settled)."""
    from src.db import TrackedPosition
    with SessionLocal() as session:
        pos = session.get(TrackedPosition, pos_id)
        if pos is None:
            raise HTTPException(404, f"no tracked position {pos_id}")
        pos.closed_at = utcnow()
        pos.close_note = note
        session.commit()
    return {"closed": pos_id, "note": note}


@app.post("/api/bots/restore")
def bots_restore(payload: dict):
    """Re-insert archived bot positions after a DB wipe. Shared logic in
    src.bots.restore_positions (the boot self-heal uses the same code with
    the committed canonical archive, so this endpoint is now a manual
    override rather than the primary recovery path)."""
    from src.bots import restore_positions
    return restore_positions(payload)


@app.get("/api/live-signals")
def live_signals(match_id: str | None = Query(None),
                 limit: int = Query(30, ge=1, le=200)):
    """In-play BUY/SELL signals on watched markets, newest first. The live
    box polls this to toast fresh signals and badge watched rows; Discord
    gets the same pushes server-side, so nothing depends on a page being
    open. Optional ?match_id= narrows to one match."""
    from src.db import LiveSignal
    with SessionLocal() as session:
        q = select(LiveSignal).order_by(LiveSignal.created_at.desc()).limit(limit)
        if match_id:
            q = select(LiveSignal).where(LiveSignal.match_id == match_id) \
                .order_by(LiveSignal.created_at.desc()).limit(limit)
        rows = session.execute(q).scalars().all()
    return {"min_diff": config.LIVE_SIGNAL_MIN_DIFF,
            "signals": [
                {
                    "id": r.id,
                    "match_id": r.match_id,
                    "market_id": r.market_id,
                    "market_title": r.market_title,
                    "side": r.side,
                    "kind": r.kind or "watched",
                    "live_probability": r.live_probability,
                    "market_probability": r.market_probability,
                    "difference": r.difference,
                    "minute": r.minute,
                    "fired_at": r.created_at.isoformat(),
                } for r in rows
            ]}


@app.get("/api/alerts/recent")
def recent_alerts(limit: int = Query(20, ge=1, le=100)):
    """Notification feed: every ripeness alert that has fired."""
    with SessionLocal() as session:
        rows = session.execute(
            select(TimingAlert).order_by(TimingAlert.created_at.desc()).limit(limit)
        ).scalars().all()
    return {"alerts": [
        {
            "match_id": r.match_id,
            "market_id": r.market_id,
            "market_title": r.market_title,
            "score": r.score,
            "decimal_odds": r.decimal_odds,
            "edge": r.edge,
            "reasons": r.reasons,
            "fired_at": r.created_at.isoformat(),
        } for r in rows
    ]}


# ---------------------------------------------------------------------------
class SettingsIn(BaseModel):
    min_edge: float | None = None
    min_confidence: float | None = None
    min_volume: float | None = None


@app.get("/api/settings")
def get_settings():
    with SessionLocal() as session:
        return {
            "min_edge": get_setting(session, "min_edge", config.MIN_EDGE),
            "min_confidence": get_setting(session, "min_confidence", config.MIN_CONFIDENCE),
            "min_volume": get_setting(session, "min_volume", config.MIN_VOLUME_24H),
        }


@app.post("/api/settings")
def update_settings(body: SettingsIn):
    with SessionLocal() as session:
        for key, value in body.model_dump(exclude_none=True).items():
            set_setting(session, key, value)
    return {"status": "saved", **get_settings()}
