"""Live team news: absences, lineup RELEASE state, and in-play events.

Son asked for "live news". This is the buildable, verifiable form of it:
who is reported missing, whether an XI has actually been announced, and
what has happened on the pitch — each with its provider, our capture
clock, and an explicit freshness state.

WHAT THIS IS NOT, AND WILL NOT BECOME
=====================================
Not a model feature. No PredictionRun, no canonical T-10 lock, no paper
signal and no engine-signature module reads anything in this module or
its tables; `tests/test_team_news_isolation.py` fails if one starts to.
The platform already MEASURED lineup and key-attacker features and found
them negative-or-marginal (key-attacker +0.0034, not significant) and
switched them off — see research_archive/mls_targeted_lineup_features_
2026-07-24.json. Rebuilding them under a "news" label would repeat a
settled experiment while hiding it behind a different word.

There is deliberately NO aggregate: no absence count folded onto a
fixture, no severity weighting, no "team strength impact". An aggregate
of absences is a model wearing a news label, and it is precisely the
thing already measured not to work.

THE PROVIDER'S WORDS STAY THE PROVIDER'S
========================================
`provider_type` and `provider_reason` are stored verbatim — "Missing
Fixture", "Knee Injury". Those are API-Football's claims about a player,
not ours. Injury feeds are stale and wrong often enough that a record is
only worth having if a reader can attribute and discount it, so nothing
is re-worded into our vocabulary and every row names its source and when
we fetched it.

TWO PROVIDERS, NEVER POOLED
===========================
Measured 2026-07-30 (research_archive/team_news_coverage_2026-07-30*.json):

  API-Football  LEAGUE lineups. 0 results more than an hour before
                kickoff; populated once a match is under way. Injuries
                exist for MLS and fill up toward kickoff. Liga MX
                injuries measured EMPTY even on completed fixtures.
  ESPN          FRIENDLY lineups, via club.friendly/summary -> rosters.
                Real XIs with formations, live and after full time.

`provider` is part of the identity of every lineup row on purpose. An
undifferentiated "lineup" with the source lost makes a coverage number
unreadable and lets one provider's silence look like the other's answer.

COUNT THE NAMES, NOT THE CONTAINERS
===================================
docs/APIFOOTBALL-TRIAL.md's governing rule — assert a non-zero count,
never a status code — is necessary and NOT sufficient here. Both
providers ship present-but-empty containers:

  API-Football  4 of 5 completed friendlies returned a NON-EMPTY lineup
                array whose side objects carried formation=None and
                startXI=0.
  ESPN          Atletico Madrid v Getafe at FT returned rosters=2 with
                named=0 on both sides.

Counting arrays reports coverage that does not exist. So release is
decided by counting NAMED PLAYERS at the leaves, and a present-but-empty
container is NOT RELEASED — never "no players are playing", never an
absence claim. That distinction is the one the frontend already tests
(e2e/contract-deterministic.spec.ts: "an UNRELEASED lineup never renders
absences as fact").

FOUR STATES, KEPT APART
=======================
    ok           the array had elements we could count
    empty        the fetch succeeded and the array was empty
    unavailable  the fetch did not succeed (transport, HTTP, no key)
    stale        derived from age: a successful read, too old to trust

`record_count` is NULL — never 0 — when a fetch was unavailable. Zero is
a measurement; NULL is the absence of one. This repo has read a zero as
an answer twice (`results: 0` on a live 45th-minute match; `{"created":
0}` masking every paper fill over a full volume), so the difference is
structural, not stylistic.

NO ALERTING IS BUILT HERE. Not a single dispatch call. "Player X is
injured" on the act-now channel is a market-relevant claim, the money
lock governs dispatch, and Son has already made one narrow concession
today; widening it is not an implementer's call. See the report for what
alerting would require.

NOTHING HERE POLLS ON A SCHEDULE. The capture functions are explicit
calls. Wiring a scheduler job spends a shared API key's budget and
starts an outward behaviour on deploy, which is Son's decision, not this
branch's.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.live import lineups
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, TeamAbsence, TeamNewsCapture,
                             TeamNewsEvent, TeamNewsLineup)

PROVIDER_APIFOOTBALL = "apifootball"
PROVIDER_ESPN = "espn"

PARSER_VERSION = "team-news-v1"

APIF_BASE = os.getenv("APIFOOTBALL_BASE", "https://v3.football.api-sports.io")
APIF_KEY_FILE = Path.home() / ".apifootball_key"
ESPN_SOCCER_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_FRIENDLY_SLUG = "club.friendly"

REDACTED = "<REDACTED>"

# How old a successful read may be before it is reported STALE. These are
# about how fast the underlying truth moves, not about how often we poll:
# an events feed goes out of date in seconds, a released XI does not.
STALE_AFTER_SECONDS = {
    "absences": 6 * 3600,     # squad news churns over hours
    "lineup": 20 * 60,        # matters most in the hour before kickoff
    "events": 120,            # an in-play feed is worthless when minutes old
}

# A courtesy throttle and an in-process ceiling. The PRO plan allows 300/min
# and 7,500/day, and THE KEY IS SHARED with other work, so this module keeps
# its own budget rather than assuming it owns the quota. The counter is
# in-process and resets when the process does — it is a courtesy guard, NOT
# an accounting record, and is reported as such.
THROTTLE_SECONDS = 0.35
REQUEST_CEILING = int(os.getenv("TEAM_NEWS_REQUEST_CEILING", "500"))
_budget_lock = threading.Lock()
_requests_used = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt):
    """Make a naive datetime explicit rather than guessing later. SQLite
    round-trips drop tzinfo, and comparing a naive read against an aware
    now() raises — which would turn a freshness question into a 500."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# credentials — the key travels in a header and nowhere else
# ---------------------------------------------------------------------------

def _apifootball_key() -> tuple[str, str]:
    """(key, source). `APIFOOTBALL_KEY` wins, else ~/.apifootball_key at
    mode 600. A group- or world-readable secret file is REFUSED rather
    than read: using it would quietly normalise leaving it readable.

    The repo's older `API_FOOTBALL_KEY` is deliberately NOT a fallback —
    it may still hold the free-tier key, which was measured season-blind
    for current seasons, and silently measuring the wrong plan is the
    failure docs/APIFOOTBALL-TRIAL.md exists to prevent.
    """
    env = os.getenv("APIFOOTBALL_KEY", "").strip()
    if env:
        return env, "APIFOOTBALL_KEY"
    try:
        if not APIF_KEY_FILE.exists():
            return "", "nowhere"
        if APIF_KEY_FILE.stat().st_mode & 0o077:
            return "", "refused_insecure_mode"
        return APIF_KEY_FILE.read_text(encoding="utf-8").strip(), \
            APIF_KEY_FILE.name
    except OSError:
        return "", "unreadable"


def _redact(text, key: str = "") -> str:
    """Strip the key from anything bound for a log line or the database.

    The key is only ever sent as a header, but a provider error string or
    a requests exception can echo one back, and `note` is persisted.
    """
    out = str(text)
    k = key or _apifootball_key()[0]
    if k:
        out = out.replace(k, REDACTED)
        if k.strip() and k.strip() != k:
            out = out.replace(k.strip(), REDACTED)
    return out


# ---------------------------------------------------------------------------
# the fetch layer — state is never inferred from a status code
# ---------------------------------------------------------------------------

class Fetched:
    """One provider read, with its state kept separate from its contents.

    `state` is 'ok' | 'empty' | 'unavailable'. It is decided by counting
    the array the provider actually shipped, never by the status code and
    never by the provider's own `results` field. A disagreement between
    that field and the array is recorded on `declared` — the array wins.
    """

    __slots__ = ("state", "items", "note", "declared", "content_hash",
                 "payload_bytes")

    def __init__(self, state: str, items: list | None = None,
                 note: str | None = None, declared: int | None = None,
                 content_hash: str | None = None,
                 payload_bytes: int | None = None):
        self.state = state
        self.items = items or []
        self.note = note
        self.declared = declared
        self.content_hash = content_hash
        self.payload_bytes = payload_bytes

    @property
    def count(self) -> int | None:
        """NULL-shaped for an unavailable read. An unavailable fetch saw
        no list at all, and reporting 0 would claim we saw an empty one."""
        return None if self.state == "unavailable" else len(self.items)

    def __repr__(self) -> str:            # pragma: no cover - debug aid
        return f"<Fetched {self.state} n={self.count}>"


def _response_items(body) -> list:
    """The `response` array, or [] for any other shape. A dict or a string
    response is NOT one item — it is a shape we did not expect, and
    counting it as one would invent a record."""
    if not isinstance(body, dict):
        return []
    r = body.get("response")
    return r if isinstance(r, list) else []


def budget_used() -> int:
    return _requests_used


def _spend() -> bool:
    global _requests_used
    with _budget_lock:
        if _requests_used >= REQUEST_CEILING:
            return False
        _requests_used += 1
    return True


def _apif_get(path: str, params: dict) -> Fetched:
    key, source = _apifootball_key()
    if not key:
        return Fetched("unavailable",
                       note=f"no API-Football key ({source})")
    if not _spend():
        return Fetched("unavailable",
                       note=f"in-process request ceiling "
                            f"{REQUEST_CEILING} reached")
    time.sleep(THROTTLE_SECONDS)
    try:
        r = requests.get(f"{APIF_BASE}/{path}", params=params, timeout=25,
                         headers={"x-apisports-key": key,
                                  "Accept": "application/json"})
    except requests.RequestException as exc:
        return Fetched("unavailable",
                       note=f"transport: {_redact(exc, key)[:240]}")
    if r.status_code // 100 != 2:
        return Fetched("unavailable", note=f"HTTP {r.status_code}")
    raw = r.text or ""
    try:
        body = r.json()
    except ValueError:
        return Fetched("unavailable", note="response was not JSON")
    items = _response_items(body)
    declared = body.get("results") if isinstance(body, dict) else None
    note = None
    if isinstance(declared, int) and declared != len(items):
        note = (f"provider results={declared} disagrees with array length "
                f"{len(items)}; THE ARRAY WINS")
    perrs = body.get("errors") if isinstance(body, dict) else None
    if perrs and not (isinstance(perrs, list) and not perrs):
        note = ((note + " | ") if note else "") + \
            f"provider errors: {_redact(json.dumps(perrs), key)[:160]}"
    return Fetched("ok" if items else "empty", items, note,
                   declared if isinstance(declared, int) else None,
                   hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                   len(raw.encode("utf-8")))


def _espn_summary(event_id: str, slug: str) -> Fetched:
    """A raw ESPN summary, wrapped in the same state vocabulary. `items`
    carries the one summary dict when the fetch succeeded."""
    try:
        r = requests.get(f"{ESPN_SOCCER_BASE}/{slug}/summary",
                         params={"event": event_id}, timeout=25,
                         headers={"User-Agent": "trivela-team-news/1.0"})
    except requests.RequestException as exc:
        return Fetched("unavailable", note=f"transport: {str(exc)[:240]}")
    if r.status_code // 100 != 2:
        return Fetched("unavailable", note=f"HTTP {r.status_code}")
    raw = r.text or ""
    try:
        body = r.json()
    except ValueError:
        return Fetched("unavailable", note="response was not JSON")
    if not isinstance(body, dict) or not body:
        return Fetched("empty", note="summary body was empty")
    return Fetched("ok", [body], None, None,
                   hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                   len(raw.encode("utf-8")))


# ---------------------------------------------------------------------------
# pure parsing — canned-testable, no network, no database
# ---------------------------------------------------------------------------

def _norm_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or "unknown"


def parse_absences(items: list) -> list[dict]:
    """API-Football /injuries response -> absence records.

    `provider_type` and `provider_reason` are copied VERBATIM. No mapping
    table, no severity scale, no normalisation into our own words.
    """
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        p = it.get("player") or {}
        t = it.get("team") or {}
        pid = p.get("id")
        name = p.get("name")
        out.append({
            "provider_player_id": (str(pid) if pid is not None else None),
            # the uniqueness key must always exist — see the migration's
            # note on PostgreSQL treating NULLs as distinct
            "player_key": (str(pid) if pid is not None
                           else f"name:{_norm_key(name)}"),
            "player_name": name,
            "provider_team_id": (str(t.get("id"))
                                 if t.get("id") is not None else None),
            "team_name": t.get("name"),
            "provider_type": p.get("type"),
            "provider_reason": p.get("reason"),
        })
    return out


def parse_events(items: list) -> list[dict]:
    """API-Football /fixtures/events response -> event records, each with
    the provider's minute preserved as given (elapsed + extra kept apart,
    because '90+4' is not minute 94 in the provider's own model)."""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tm = it.get("time") or {}
        team = it.get("team") or {}
        pl = it.get("player") or {}
        ast = it.get("assist") or {}
        elapsed = tm.get("elapsed")
        extra = tm.get("extra")
        etype = it.get("type")
        detail = it.get("detail")
        rec = {
            "provider_minute": elapsed if isinstance(elapsed, int) else None,
            "provider_minute_extra": (extra if isinstance(extra, int)
                                      else None),
            "event_type": etype,
            "event_detail": detail,
            "provider_team_id": (str(team.get("id"))
                                 if team.get("id") is not None else None),
            "team_name": team.get("name"),
            "provider_player_id": (str(pl.get("id"))
                                   if pl.get("id") is not None else None),
            "player_name": pl.get("name"),
            "assist_name": ast.get("name"),
        }
        rec["event_key"] = "|".join(str(x) for x in (
            rec["provider_minute"], rec["provider_minute_extra"],
            etype, detail, _norm_key(rec["player_name"]),
            _norm_key(rec["team_name"])))[:160]
        out.append(rec)
    return out


def espn_lineup_states(summary: dict) -> dict:
    """ESPN summary -> per-side release state, by COUNTING NAMES.

    Reuses `lineups.parse_lineup` for the ESPN roster shape rather than
    re-deriving it: that parser already encodes where starters, positions
    and formations live, and forking it would fork its fixes. What is
    added on top is the name count, because `parse_lineup` was written
    for a league feed where a present roster means a real roster.

    THE CASE THIS EXISTS FOR: rosters can be present with two entries and
    carry zero names (Atletico Madrid v Getafe, FT, 2026-07-30). That is
    NOT RELEASED, not an empty lineup, and not an absence claim.
    """
    parsed = lineups.parse_lineup(summary if isinstance(summary, dict) else {})
    names = _espn_team_names(summary)
    out = {}
    for side in ("home", "away"):
        sd = parsed.get(side)
        if sd is None:
            # no container at all -> this fixture has no roster coverage
            out[side] = {"container_present": False, "named": 0,
                         "starters": 0, "released": False,
                         "formation": None, "team_name": names.get(side)}
            continue
        entries = sd.get("entries") or []
        named = [e for e in entries if e.get("name")]
        starters = [e for e in named if e.get("starter")]
        out[side] = {
            "container_present": True,
            "named": len(named),
            "starters": len(starters),
            # the same threshold src/live/lineups.py uses for `confirmed`,
            # so two modules cannot disagree about what "released" means
            "released": len(starters) >= 11,
            "formation": sd.get("formation"),
            "team_name": names.get(side),
        }
    return out


def _espn_team_names(summary) -> dict:
    out = {}
    if not isinstance(summary, dict):
        return out
    comps = ((summary.get("header") or {}).get("competitions") or [{}])
    for c in (comps[0].get("competitors") or []):
        if c.get("homeAway") in ("home", "away"):
            out[c["homeAway"]] = ((c.get("team") or {}).get("displayName")
                                 or (c.get("team") or {}).get("name"))
    return out


def apifootball_lineup_states(items: list) -> dict:
    """API-Football /fixtures/lineups response -> per-side release state.

    Same rule as ESPN: count the names in `startXI`. A side object with
    formation=None and startXI=[] is a present-but-empty container, which
    is how this provider represents a friendly it has no detail for — 4
    of 5 completed friendlies looked exactly like that on 2026-07-30.

    Sides come back as an ARRAY with no home/away marker, so the first
    element is home and the second away only by API-Football's documented
    ordering. Rather than assert that, sides are keyed by position and the
    team name is carried, so a consumer can verify the mapping itself.
    """
    out = {}
    for i, it in enumerate(items[:2]):
        if not isinstance(it, dict):
            continue
        side = "home" if i == 0 else "away"
        xi = it.get("startXI")
        xi = xi if isinstance(xi, list) else []
        named = [e for e in xi
                 if isinstance(e, dict) and ((e.get("player") or {}).get("name"))]
        team = it.get("team") or {}
        out[side] = {
            "container_present": True,
            "named": len(named),
            "starters": len(named),
            "released": len(named) >= 11,
            "formation": it.get("formation"),
            "team_name": team.get("name"),
        }
    for side in ("home", "away"):
        out.setdefault(side, {"container_present": False, "named": 0,
                              "starters": 0, "released": False,
                              "formation": None, "team_name": None})
    return out


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------

def freshness(feed: str, captured_at, state: str, now=None) -> dict:
    """The freshness block every payload carries.

    Four outcomes, and they are not interchangeable:
      unavailable  the last fetch failed — we do not know the truth
      empty        the provider answered with nothing. For a lineup that
                   means NOT RELEASED; for absences it means the provider
                   listed none, which is NOT "nobody is injured"
      stale        a real read, older than this feed's tolerance
      ok           a real read, inside tolerance
    `never_captured` is its own state: no attempt has been recorded, which
    is different again from an attempt that came back empty.
    """
    now = now or _now()
    ca = _utc(captured_at)
    if ca is None:
        return {"state": "never_captured", "captured_at": None,
                "age_seconds": None, "stale": None,
                "stale_after_seconds": STALE_AFTER_SECONDS.get(feed),
                "means": "no capture attempt has been recorded for this feed"}
    age = (now - ca).total_seconds()
    limit = STALE_AFTER_SECONDS.get(feed)
    stale = bool(limit is not None and age > limit)
    if state == "unavailable":
        means = ("the last fetch did not succeed; the truth is UNKNOWN, "
                 "not empty")
    elif state == "empty":
        means = ("the provider answered with an empty list. This is not a "
                 "claim that the list is truly empty")
    elif stale:
        means = "a real read, but older than this feed's tolerance"
    else:
        means = "a real read, inside this feed's tolerance"
    return {"state": ("stale" if (stale and state != "unavailable")
                      else state),
            "raw_state": state,
            "captured_at": ca.isoformat(),
            "age_seconds": round(age, 1),
            "stale": stale,
            "stale_after_seconds": limit,
            "means": means}


# ---------------------------------------------------------------------------
# persistence — every write is an UPSERT, so a re-read refreshes
# ---------------------------------------------------------------------------

def _record_capture(s, provider: str, subject_ref: str, feed: str,
                    f: Fetched, fixture_id=None, competition=None) -> None:
    row = (s.query(TeamNewsCapture)
           .filter_by(provider=provider, subject_ref=str(subject_ref),
                      feed=feed).first())
    now = _now()
    if row is None:
        row = TeamNewsCapture(provider=provider,
                              subject_ref=str(subject_ref), feed=feed,
                              first_captured_at=now, attempts=0)
        s.add(row)
    row.state = f.state
    # NULL, not 0, for an unavailable read — Fetched.count enforces this
    row.record_count = f.count
    row.provider_declared_count = f.declared
    row.captured_at = now
    row.attempts = (row.attempts or 0) + 1
    row.note = (_redact(f.note)[:300] if f.note else None)
    row.content_hash = f.content_hash
    row.payload_bytes = f.payload_bytes
    if fixture_id is not None:
        row.fixture_id = fixture_id
    if competition:
        row.competition = competition


def _dormant(feed: str) -> dict:
    """The live plane is off. That is not an empty feed and not a failed
    fetch — it is 'we did not look', and it gets its own word."""
    return {"state": "plane_dormant", "feed": feed, "stored": 0,
            "means": ("the live plane is not ready, so nothing was fetched "
                      "or stored. This is not an empty result")}


def capture_absences(subject_ref: str, fixture_id: int | None = None,
                     competition: str | None = None,
                     items: list | None = None) -> dict:
    """Ingest provider-reported absences for one fixture.

    `items` accepts a canned response so the parse/persist path is
    testable without the network.
    """
    if not plane_ready():
        return _dormant("absences")
    f = (Fetched("ok" if items else "empty", items or [])
         if items is not None
         else _apif_get("injuries", {"fixture": subject_ref}))
    s = get_session()
    try:
        records = parse_absences(f.items)
        now = _now()
        stored = 0
        for rec in records:
            row = (s.query(TeamAbsence)
                   .filter_by(provider=PROVIDER_APIFOOTBALL,
                              subject_ref=str(subject_ref),
                              player_key=rec["player_key"]).first())
            if row is None:
                row = TeamAbsence(provider=PROVIDER_APIFOOTBALL,
                                  subject_ref=str(subject_ref),
                                  player_key=rec["player_key"],
                                  first_seen_at=now)
                s.add(row)
            for k in ("provider_player_id", "player_name",
                      "provider_team_id", "team_name", "provider_type",
                      "provider_reason"):
                setattr(row, k, rec[k])
            row.captured_at = now
            row.last_seen_at = now
            row.fixture_id = fixture_id
            row.competition = competition
            stored += 1
        _record_capture(s, PROVIDER_APIFOOTBALL, subject_ref, "absences", f,
                        fixture_id, competition)
        s.commit()
        return {"state": f.state, "feed": "absences", "count": f.count,
                "stored": stored, "note": (_redact(f.note) if f.note
                                           else None)}
    except Exception as exc:
        s.rollback()
        print(f"[team_news] absences {subject_ref}: {_redact(exc)}")
        return {"state": "unavailable", "feed": "absences", "count": None,
                "stored": 0, "note": f"persist failed: {_redact(exc)[:200]}"}
    finally:
        s.close()


def capture_events(subject_ref: str, fixture_id: int | None = None,
                   competition: str | None = None,
                   items: list | None = None) -> dict:
    """Ingest in-play events for one fixture.

    Exposed for later consumption by the in-play detector on
    feat-hunter-live-stats. This module does not import, read or modify
    src/live/hunter.py, and does not decide anything from these rows.
    """
    if not plane_ready():
        return _dormant("events")
    f = (Fetched("ok" if items else "empty", items or [])
         if items is not None
         else _apif_get("fixtures/events", {"fixture": subject_ref}))
    s = get_session()
    try:
        now = _now()
        stored = 0
        for rec in parse_events(f.items):
            row = (s.query(TeamNewsEvent)
                   .filter_by(provider=PROVIDER_APIFOOTBALL,
                              subject_ref=str(subject_ref),
                              event_key=rec["event_key"]).first())
            if row is None:
                row = TeamNewsEvent(provider=PROVIDER_APIFOOTBALL,
                                    subject_ref=str(subject_ref),
                                    event_key=rec["event_key"],
                                    first_seen_at=now)
                s.add(row)
            for k in ("provider_minute", "provider_minute_extra",
                      "event_type", "event_detail", "provider_team_id",
                      "team_name", "provider_player_id", "player_name",
                      "assist_name"):
                setattr(row, k, rec[k])
            # the provider's minute AND our clock — both, never one
            row.captured_at = now
            row.fixture_id = fixture_id
            row.competition = competition
            stored += 1
        _record_capture(s, PROVIDER_APIFOOTBALL, subject_ref, "events", f,
                        fixture_id, competition)
        s.commit()
        return {"state": f.state, "feed": "events", "count": f.count,
                "stored": stored, "note": (_redact(f.note) if f.note
                                           else None)}
    except Exception as exc:
        s.rollback()
        print(f"[team_news] events {subject_ref}: {_redact(exc)}")
        return {"state": "unavailable", "feed": "events", "count": None,
                "stored": 0, "note": f"persist failed: {_redact(exc)[:200]}"}
    finally:
        s.close()


def _store_lineup_states(s, provider: str, subject_ref: str, states: dict,
                         fixture_id, competition, kickoff_utc) -> dict:
    """Upsert per-side release state and freeze the release TRANSITION.

    `first_released_at` is written ONCE, on the first capture that sees a
    full XI, and never rewritten: the value of the transition is when the
    news actually landed, and a later re-read must not move that clock.
    `released_minutes_before_kickoff` is frozen at the same moment so a
    subsequent reschedule cannot retroactively change how early it landed.
    """
    now = _now()
    out = {}
    for side in ("home", "away"):
        st = states.get(side)
        if st is None:
            continue
        row = (s.query(TeamNewsLineup)
               .filter_by(provider=provider, subject_ref=str(subject_ref),
                          side=side).first())
        if row is None:
            row = TeamNewsLineup(provider=provider,
                                 subject_ref=str(subject_ref), side=side,
                                 first_seen_at=now)
            s.add(row)
        row.team_name = st.get("team_name") or row.team_name
        row.formation = st.get("formation")
        row.named_count = st.get("named")
        row.starter_count = st.get("starters")
        row.container_present = bool(st.get("container_present"))
        row.released = bool(st.get("released"))
        row.captured_at = now
        row.fixture_id = fixture_id
        row.competition = competition
        if kickoff_utc is not None:
            row.kickoff_utc = kickoff_utc
        newly_released = False
        if row.released and row.first_released_at is None:
            row.first_released_at = now
            newly_released = True
            ko = _utc(kickoff_utc or row.kickoff_utc)
            if ko is not None:
                row.released_minutes_before_kickoff = int(
                    round((ko - now).total_seconds() / 60))
        out[side] = {
            "released": row.released,
            "container_present": row.container_present,
            "named": row.named_count,
            "formation": row.formation,
            "team_name": row.team_name,
            "newly_released": newly_released,
            "first_released_at": (_utc(row.first_released_at).isoformat()
                                  if row.first_released_at else None),
            "released_minutes_before_kickoff":
                row.released_minutes_before_kickoff,
            "lineup_state": _lineup_state_word(row.container_present,
                                               row.released),
        }
    return out


def _lineup_state_word(container_present: bool, released: bool) -> str:
    """The three states that must never collapse into one another."""
    if released:
        return "released"
    if container_present:
        # the container exists and the XI does not. NOT an absence claim.
        return "not_released"
    return "no_coverage"


def capture_friendly_lineup(espn_event_id: str, kickoff_utc=None,
                            competition: str = "club-friendly",
                            summary: dict | None = None) -> dict:
    """Capture a CLUB FRIENDLY's lineup release state from ESPN.

    Friendlies are where this matters most: a league side fields roughly
    its best XI predictably, while a friendly XI is arbitrary, so the
    release is the largest genuine information event on the fixture — and
    the one thing about a friendly that is information rather than
    forecast.

    The fixture is identified by its ESPN EVENT ID, which is the identity
    src/friendlies.py already keys every friendly on (its scoreboard and
    schedule parse straight to it). No team-name-plus-date join happens
    here; that join is exactly what the friendlies identity rules ban.

    Writes to this module's own tables only. It deliberately does NOT
    write a LineupSnapshot: that table is referenced by canonical T-10
    locks, and putting news-sourced rows on the lock's provenance path is
    precisely the coupling this branch must not create.
    """
    if not plane_ready():
        return _dormant("lineup")
    f = (Fetched("ok", [summary]) if summary is not None
         else _espn_summary(str(espn_event_id), ESPN_FRIENDLY_SLUG))
    s = get_session()
    try:
        body = f.items[0] if f.items else {}
        states = (espn_lineup_states(body) if f.state == "ok" else {})
        sides = _store_lineup_states(s, PROVIDER_ESPN, espn_event_id, states,
                                     None, competition, kickoff_utc)
        _record_capture(s, PROVIDER_ESPN, espn_event_id, "lineup", f,
                        None, competition)
        s.commit()
        return {"state": f.state, "feed": "lineup",
                "provider": PROVIDER_ESPN, "sides": sides,
                "note": (_redact(f.note) if f.note else None)}
    except Exception as exc:
        s.rollback()
        print(f"[team_news] friendly lineup {espn_event_id}: {_redact(exc)}")
        return {"state": "unavailable", "feed": "lineup",
                "provider": PROVIDER_ESPN, "sides": {},
                "note": f"persist failed: {_redact(exc)[:200]}"}
    finally:
        s.close()


def capture_league_lineup(subject_ref: str, fixture_id: int | None = None,
                          competition: str | None = None, kickoff_utc=None,
                          items: list | None = None) -> dict:
    """Capture a LEAGUE fixture's lineup release state from API-Football.

    Measured behaviour: 0 results more than an hour before kickoff, and a
    populated array once the match is under way. So an `empty` answer here
    is overwhelmingly "not released yet" and is reported as such — never
    as an absence claim, and never as a released-but-empty XI.
    """
    if not plane_ready():
        return _dormant("lineup")
    f = (Fetched("ok" if items else "empty", items or [])
         if items is not None
         else _apif_get("fixtures/lineups", {"fixture": subject_ref}))
    s = get_session()
    try:
        states = (apifootball_lineup_states(f.items) if f.state == "ok"
                  else {})
        sides = _store_lineup_states(s, PROVIDER_APIFOOTBALL, subject_ref,
                                     states, fixture_id, competition,
                                     kickoff_utc)
        _record_capture(s, PROVIDER_APIFOOTBALL, subject_ref, "lineup", f,
                        fixture_id, competition)
        s.commit()
        return {"state": f.state, "feed": "lineup",
                "provider": PROVIDER_APIFOOTBALL, "sides": sides,
                "note": (_redact(f.note) if f.note else None)}
    except Exception as exc:
        s.rollback()
        print(f"[team_news] league lineup {subject_ref}: {_redact(exc)}")
        return {"state": "unavailable", "feed": "lineup",
                "provider": PROVIDER_APIFOOTBALL, "sides": {},
                "note": f"persist failed: {_redact(exc)[:200]}"}
    finally:
        s.close()


# ---------------------------------------------------------------------------
# the read surface
# ---------------------------------------------------------------------------

def _capture_row(s, subject_ref: str, feed: str, provider: str | None = None):
    q = s.query(TeamNewsCapture).filter_by(subject_ref=str(subject_ref),
                                           feed=feed)
    if provider:
        q = q.filter_by(provider=provider)
    return q.order_by(TeamNewsCapture.captured_at.desc()).first()


def fixture_news(subject_ref: str) -> dict:
    """Everything known about one fixture's team news, with provenance.

    Read-only. Every block states its provider, when it was captured, how
    old that is, and which of the freshness states it is in. Absent data
    is reported as absent — the three ways a block can carry nothing
    (unavailable / empty / never captured) are never collapsed, because a
    reader who cannot tell them apart cannot tell "nobody is injured"
    from "we could not ask".
    """
    ref = str(subject_ref)
    out: dict = {
        "fixture_ref": ref,
        "generated_at": _now().isoformat(),
        "_not_a_model_input": (
            "Team news is reader context. No prediction run, no T-10 lock "
            "and no paper signal reads any of it; lineup and key-attacker "
            "features were measured negative-or-marginal (key-attacker "
            "+0.0034, not significant) and are off."),
        "_provider_words": (
            "type and reason are the provider's own strings, stored "
            "verbatim. They are that provider's claim, not ours."),
    }
    if not plane_ready():
        out["availability"] = _dormant("all")
        return out
    s = get_session()
    try:
        # ---- absences -----------------------------------------------------
        cap = _capture_row(s, ref, "absences")
        rows = (s.query(TeamAbsence).filter_by(subject_ref=ref)
                .order_by(TeamAbsence.player_name).all())
        newest = _utc(cap.captured_at) if cap else None
        recs = []
        for r in rows:
            last = _utc(r.last_seen_at)
            recs.append({
                "player_name": r.player_name,
                "provider_player_id": r.provider_player_id,
                "team_name": r.team_name,
                # verbatim, and labelled as the provider's
                "provider_type": r.provider_type,
                "provider_reason": r.provider_reason,
                "provider": r.provider,
                "captured_at": (_utc(r.captured_at).isoformat()
                                if r.captured_at else None),
                "first_seen_at": (_utc(r.first_seen_at).isoformat()
                                  if r.first_seen_at else None),
                "last_seen_at": (last.isoformat() if last else None),
                # a record the provider has stopped reporting is kept and
                # FLAGGED, not deleted: a retraction is information too
                "still_reported": (None if (newest is None or last is None)
                                   else last >= newest),
            })
        out["absences"] = {
            "provider": (cap.provider if cap else PROVIDER_APIFOOTBALL),
            "records": recs,
            "record_count": (cap.record_count if cap else None),
            "stored_records": len(recs),
            "freshness": freshness("absences",
                                   cap.captured_at if cap else None,
                                   cap.state if cap else "unavailable"),
            "note": (cap.note if cap else None),
            "_empty_is_not_nobody_injured": (
                "An empty or absent list means the provider listed no one. "
                "It is NOT evidence that no player is unavailable."),
        }

        # ---- lineups, PER PROVIDER, never pooled --------------------------
        lus = (s.query(TeamNewsLineup).filter_by(subject_ref=ref).all())
        by_provider: dict = {}
        for r in lus:
            blk = by_provider.setdefault(r.provider, {"sides": {}})
            blk["sides"][r.side] = {
                "team_name": r.team_name,
                "formation": r.formation,
                "named_count": r.named_count,
                "starter_count": r.starter_count,
                "lineup_state": _lineup_state_word(bool(r.container_present),
                                                   bool(r.released)),
                "released": bool(r.released),
                "container_present": bool(r.container_present),
                "first_released_at": (_utc(r.first_released_at).isoformat()
                                      if r.first_released_at else None),
                "released_minutes_before_kickoff":
                    r.released_minutes_before_kickoff,
                "captured_at": (_utc(r.captured_at).isoformat()
                                if r.captured_at else None),
            }
        for prov, blk in by_provider.items():
            c = _capture_row(s, ref, "lineup", prov)
            blk["freshness"] = freshness("lineup",
                                         c.captured_at if c else None,
                                         c.state if c else "unavailable")
            blk["note"] = (c.note if c else None)
        out["lineup"] = {
            "by_provider": by_provider,
            "_states": {
                "released": "a full XI exists and is named",
                "not_released": ("a roster container exists with no XI in "
                                 "it. NOT an absence claim, NOT 'no players "
                                 "are playing'"),
                "no_coverage": "this provider carries no roster for this "
                               "fixture at all",
            },
            "_unreleased_licenses_nothing": (
                "Before a lineup is released the only honest statement is "
                "'not yet released'. An absent XI is never a claim that "
                "anyone is out."),
        }

        # ---- events -------------------------------------------------------
        ec = _capture_row(s, ref, "events")
        evs = (s.query(TeamNewsEvent).filter_by(subject_ref=ref)
               .order_by(TeamNewsEvent.provider_minute,
                         TeamNewsEvent.id).all())
        out["events"] = {
            "provider": (ec.provider if ec else PROVIDER_APIFOOTBALL),
            "records": [{
                "provider_minute": e.provider_minute,
                "provider_minute_extra": e.provider_minute_extra,
                "event_type": e.event_type,
                "event_detail": e.event_detail,
                "team_name": e.team_name,
                "player_name": e.player_name,
                "assist_name": e.assist_name,
                # both clocks, always
                "captured_at": (_utc(e.captured_at).isoformat()
                                if e.captured_at else None),
                "first_seen_at": (_utc(e.first_seen_at).isoformat()
                                  if e.first_seen_at else None),
            } for e in evs],
            "record_count": (ec.record_count if ec else None),
            "stored_records": len(evs),
            "freshness": freshness("events", ec.captured_at if ec else None,
                                   ec.state if ec else "unavailable"),
            "note": (ec.note if ec else None),
        }
        return out
    finally:
        s.close()


def coverage() -> dict:
    """Per-source, per-competition coverage with its DENOMINATOR.

    A coverage number without a denominator implies completeness it has
    not earned, so every cell here is n/total with the three lineup
    states kept apart. Counts come from what is stored, which is what was
    actually observed — not from what the providers are believed to offer.
    """
    if not plane_ready():
        return _dormant("coverage")
    s = get_session()
    try:
        out: dict = {"generated_at": _now().isoformat(), "by_source": {}}
        for prov in (PROVIDER_ESPN, PROVIDER_APIFOOTBALL):
            rows = s.query(TeamNewsLineup).filter_by(provider=prov).all()
            subjects: dict = {}
            for r in rows:
                key = (r.competition or "unknown", r.subject_ref)
                st = _lineup_state_word(bool(r.container_present),
                                        bool(r.released))
                prev = subjects.get(key)
                # a fixture counts as released only when BOTH sides are;
                # one side is partial, and partial is not released
                subjects[key] = _merge_state(prev, st)
            by_comp: dict = {}
            for (comp, _ref), st in subjects.items():
                b = by_comp.setdefault(comp, {"released": 0,
                                              "not_released": 0,
                                              "no_coverage": 0, "total": 0})
                b[st] += 1
                b["total"] += 1
            caps = (s.query(TeamNewsCapture)
                    .filter_by(provider=prov, feed="lineup").all())
            out["by_source"][prov] = {
                "lineups_by_competition": by_comp,
                "lineup_capture_states": _tally(c.state for c in caps),
                "fixtures_attempted": len(caps),
            }
        acaps = (s.query(TeamNewsCapture)
                 .filter_by(feed="absences").all())
        out["absences"] = {
            "fixtures_attempted": len(acaps),
            "capture_states": _tally(c.state for c in acaps),
            "fixtures_with_records": sum(
                1 for c in acaps if (c.record_count or 0) > 0),
            "total_records_stored": s.query(TeamAbsence).count(),
        }
        ecaps = s.query(TeamNewsCapture).filter_by(feed="events").all()
        out["events"] = {
            "fixtures_attempted": len(ecaps),
            "capture_states": _tally(c.state for c in ecaps),
            "total_records_stored": s.query(TeamNewsEvent).count(),
        }
        out["_denominator_note"] = (
            "fixtures_attempted is the denominator: fixtures this module "
            "actually fetched. It is not the number of fixtures on the "
            "board, and this table does not claim to cover them.")
        out["budget"] = {
            "in_process_requests_used": budget_used(),
            "ceiling": REQUEST_CEILING,
            "_note": ("an in-process courtesy counter that resets with the "
                      "process, NOT an accounting record of the shared key"),
        }
        return out
    finally:
        s.close()


def _merge_state(prev: str | None, st: str) -> str:
    """Worst-of, so a half-released fixture never reads as released."""
    order = {"no_coverage": 0, "not_released": 1, "released": 2}
    if prev is None:
        return st
    return prev if order[prev] <= order[st] else st


def _tally(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def resolve_fixture(espn_event_id: str) -> dict | None:
    """Look up a live-plane fixture by its ESPN event id, for league
    fixtures that have one. Returns None rather than guessing: a fixture
    that cannot be resolved gets news keyed on the provider ref alone,
    which is the honest outcome for a club friendly (which has no live
    fixture row by design) as well as for an unknown id."""
    if not plane_ready():
        return None
    s = get_session()
    try:
        fx = (s.query(Fixture)
              .filter_by(espn_event_id=str(espn_event_id)).first())
        if fx is None:
            return None
        return {"fixture_id": fx.id, "competition": fx.competition_slug,
                "kickoff_utc": _utc(fx.current_kickoff_utc)}
    finally:
        s.close()
