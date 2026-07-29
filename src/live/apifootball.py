"""API-Football ingestion: measured xG coverage, per-fixture xG, club leagues.

WHY THIS EXISTS. Son's rule: a club's xG rating comes from the league that
club plays in. The friendlies surface spans the global club universe, and for
almost all of those leagues this stack has no team-level xG source at all —
`model_epl` / `model_ligamx` say so in their own docstrings, and the goals-only
ladder declares its M3 rung *unavailable* rather than merely unbuilt. A paid
API-Football key fills exactly that gap.

MLS IS EXCLUDED ON PURPOSE. MLS already has real Sportec xG, free, with a
measured effect (+0.0235 vs baseline on n=162, NOT significant), and
`src/live/mls_stats.py` + `src/live/model_mls.py` are inside the MLS engine
signature. Switching MLS to this provider would invalidate the live approval
and halt shadow collection for no measured gain. `MLS_LEAGUE_ID` is listed
here only so the guard can refuse it by name.

THREE THINGS THIS MODULE REFUSES TO DO
--------------------------------------

1. **Read a hollow value as data.** The expected-goals statistic TYPE appears
   in `fixtures/statistics` responses even where every VALUE is null. A
   presence check on the type therefore succeeds while carrying nothing — the
   same shallow-success shape as this repo's `results: 0` and
   `{"created": 0}` incidents. `parse_xg` returns None for null, '', '-',
   non-numeric AND for exactly 0: the provider uses null and 0
   interchangeably as placeholders, the two cannot be distinguished, and
   reading a placeholder 0 as "this team generated zero xG" is precisely how
   missing evidence becomes a number. The pre-registered trial criteria
   (`scripts/verify_apifootball.py`, criteria fingerprint frozen before a key
   existed) took the same line, so this is consistency, not a new judgement.

2. **Claim coverage it has not measured.** API-Football's documentation is
   403-walled; no published xG coverage list exists. Coverage is therefore
   MEASURED per (league, season) by sampling completed fixtures, and the
   measurement is stored with the date it was taken.

   The sampling is SPREAD EVENLY ACROSS THE SEASON, and that detail is
   load-bearing. A most-recent-N sample is biased: on a split-year league the
   newest completed fixtures are the end-of-season promotion/relegation
   playoff block (a distinct, later-created fixture-id range) which carries no
   xG. Measured 2026-07-29 — sampling the three most recent fixtures called
   Bundesliga, Primeira Liga and Ligue 1 'partial'; sampling across the
   season showed all three fully covered in regular-season rounds with the
   ONLY miss being the round labelled 'Final'. Reporting those leagues as
   xG-less would have been wrong, and the error was in the sampling.

3. **Overwrite evidence.** A stored xG is what the provider said WHEN WE
   ASKED. Sportmonks-style retroactive correction cannot be ruled out for a
   provider whose docs we cannot read, so the store is APPEND-ONLY: the unique
   key carries a hash of the values, so a re-read returning the same numbers
   writes nothing (idempotent, cheap to resume) and a re-read returning
   DIFFERENT numbers inserts a second row. A retroactive correction becomes
   visible and countable — see `retroactive_corrections`.

The key is a secret: env first, else ~/.apifootball_key when that file is
owner-only. It is sent as a header, never placed in a path, param or log line,
and `_redact` strips it from anything on its way to stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import config
from src.live.db import get_session, plane_ready
from src.live.models import (ApiFootballClubLeague, ApiFootballFixtureXg,
                             ApiFootballLeagueCoverage)

REDACTED = "<APIFOOTBALL_KEY_REDACTED>"
KEY_FILE = Path.home() / ".apifootball_key"

FINISHED_STATUSES = {"FT", "AET", "PEN"}

# MLS keeps Sportec — see the module docstring. Named so the guard can refuse
# it explicitly instead of relying on nobody adding the row.
MLS_LEAGUE_ID = 253

# Verdict vocabulary. `indeterminate_no_completed_fixture` is never read as
# clean: a coverage check that could not run is not a coverage check.
XG_AVAILABLE = "xg_available"
XG_PARTIAL = "xg_partial"
XG_ABSENT = "xg_absent"
XG_INDETERMINATE = "indeterminate_no_completed_fixture"

# Coverage verdicts a rating may be fitted from. `xg_partial` is included
# deliberately: partial coverage degrades gracefully because a fixture with no
# usable value simply does not contribute, and every rating carries the n that
# actually fed it. What is refused is a league measured to have NO xG.
FITTABLE_VERDICTS = (XG_AVAILABLE, XG_PARTIAL)

_XG_TYPE_REJECT = ("against", "conceded", "prevented", "allowed",
                   "opponent", "xga")


class ApiFootballError(RuntimeError):
    """Provider or configuration failure, with the key already redacted."""


class BudgetExceeded(ApiFootballError):
    """The per-process request ceiling was hit. Aborting is the point: the
    daily quota is shared with every other use of this key."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- secrets ---------------------------------------------------------------

def load_key() -> str:
    """The API key, from APIFOOTBALL_KEY or an owner-only key file.

    A group- or world-readable key file is REFUSED rather than used: reading
    it would quietly normalise leaving the secret exposed. `.strip()` is
    deliberate — dashboard copy-paste smuggles trailing newlines into secrets
    and a whitespace-damaged one already failed silently in this repo once
    (NTFY_TOPIC, 2026-07-22)."""
    env = (os.environ.get("APIFOOTBALL_KEY") or "").strip()
    if env:
        return env
    if not KEY_FILE.exists():
        raise ApiFootballError(
            "no API-Football key: set APIFOOTBALL_KEY or create "
            f"{KEY_FILE.name} (mode 600)")
    mode = KEY_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        raise ApiFootballError(
            f"{KEY_FILE} is mode {mode:03o}; it must be 600 (owner-only). "
            "Refusing to read a group- or world-readable secret.")
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise ApiFootballError(f"{KEY_FILE.name} is empty")
    return key


def _redact(text: object, key: str) -> str:
    out = str(text)
    if key:
        out = out.replace(key, REDACTED)
        if key.strip() and key.strip() != key:
            out = out.replace(key.strip(), REDACTED)
    return out


# --- payload primitives (pure) --------------------------------------------

def provider_errors(payload: object) -> list[str]:
    """API-Football reports plan/token/parameter problems INSIDE a 200 body,
    as `errors` — a dict when populated, [] when not. An HTTP 200 is not a
    success signal here."""
    if not isinstance(payload, dict):
        return ["payload is not a JSON object"]
    err = payload.get("errors")
    if not err:
        return []
    if isinstance(err, dict):
        return [f"{k}: {v}" for k, v in err.items()]
    if isinstance(err, list):
        return [str(e) for e in err]
    return [str(err)]


def response_items(payload: object) -> list:
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response")
    return resp if isinstance(resp, list) else []


def _norm_stat_type(t: object) -> str:
    return "".join(c for c in str(t or "").lower() if c.isalnum())


def is_xg_type(t: object) -> bool:
    """Is this statistic type an expected-goals FOR figure? The
    against/conceded/prevented variants are rejected: xGA is a different
    statistic and accepting it would let coverage be claimed on the strength
    of the opponent's number."""
    n = _norm_stat_type(t)
    if not n or any(bad in n for bad in _XG_TYPE_REJECT):
        return False
    return n == "xg" or "expectedgoals" in n


def parse_xg(raw: object) -> float | None:
    """A usable xG value, or None for ABSENCE.

    None / '' / '-' / 'null' / non-numeric are absence. So is exactly 0 —
    see the module docstring: the provider uses null and 0 interchangeably as
    placeholders, they cannot be distinguished, and a placeholder read as zero
    is missing evidence converted into a number. A genuine 0.00-xG team-match
    is therefore also treated as absent; that costs a true zero occasionally
    and never invents one, which is the trade this repo makes everywhere."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        s = str(raw).strip()
        if s.endswith("%"):
            s = s[:-1].strip()
        if not s or s.lower() in {"-", "–", "—", "null", "none",
                                  "n/a", "na"}:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    return v if v > 0 else None


def read_fixture_stats(payload: object) -> list[dict]:
    """Per-team xG from one `fixtures/statistics` response.

    -> [{provider_team_id, team_name, xg_type, xg_raw, xg}] in provider order.
    `xg_type` present with `xg` None is the hollow form the coverage
    measurement exists to catch, and is reported as such rather than dropped.
    """
    out: list[dict] = []
    for entry in response_items(payload):
        if not isinstance(entry, dict):
            continue
        team = entry.get("team") or {}
        stats = entry.get("statistics")
        stats = stats if isinstance(stats, list) else []
        seen_type = raw = None
        for st in stats:
            if not isinstance(st, dict):
                continue
            if seen_type is None and is_xg_type(st.get("type")):
                seen_type, raw = str(st.get("type")), st.get("value")
        out.append({"provider_team_id": team.get("id"),
                    "team_name": team.get("name"),
                    "xg_type": seen_type, "xg_raw": raw,
                    "xg": parse_xg(raw)})
    return out


def completed_fixtures(payload: object) -> list[dict]:
    """Completed fixtures from a `fixtures` response, kickoff-ascending and
    normalized. Status is the provider's claim, so FT/AET/PEN is required
    rather than inferred from a score being present."""
    out = []
    for f in response_items(payload):
        if not isinstance(f, dict):
            continue
        fx = f.get("fixture") or {}
        status = (fx.get("status") or {}).get("short")
        if status not in FINISHED_STATUSES:
            continue
        teams = f.get("teams") or {}
        goals = f.get("goals") or {}
        out.append({
            "provider_fixture_id": fx.get("id"),
            "timestamp": fx.get("timestamp"),
            "kickoff_iso": fx.get("date"),
            "round_label": (f.get("league") or {}).get("round"),
            "home_team_id": ((teams.get("home") or {}).get("id")),
            "away_team_id": ((teams.get("away") or {}).get("id")),
            "home_name": ((teams.get("home") or {}).get("name")),
            "away_name": ((teams.get("away") or {}).get("name")),
            "home_goals": goals.get("home"),
            "away_goals": goals.get("away"),
        })
    out.sort(key=lambda r: r["timestamp"] or 0)
    return out


def spread_sample(seq: list, n: int) -> list:
    """`n` elements spread EVENLY across `seq` (endpoints included).

    Not the most recent n — see the module docstring. This choice is the
    difference between measuring a league and measuring its playoff block."""
    if n <= 0 or not seq:
        return []
    if len(seq) <= n:
        return list(seq)
    if n == 1:
        return [seq[len(seq) // 2]]
    step = (len(seq) - 1) / (n - 1)
    return [seq[round(i * step)] for i in range(n)]


def value_hash(xg_raw: object, xg_against_raw: object,
               goals: object, goals_conceded: object) -> str:
    """The discriminator that turns a silent overwrite into a new row. Hashing
    the RAW provider strings (not the parsed floats) means a provider that
    changes '1.81' to '1.810' is visible too — a formatting change is still a
    change in what we were told."""
    canon = json.dumps([None if v is None else str(v)
                        for v in (xg_raw, xg_against_raw, goals,
                                  goals_conceded)],
                       separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


# --- identity (pure) -------------------------------------------------------

_GENERIC_TOKENS = {"fc", "cf", "sc", "cd", "ud", "ac", "afc", "cfc", "cp",
                   "club", "sv", "vfl", "vfb", "bsc", "tsg", "as", "ss",
                   "ssc", "ac", "us", "usl", "rc", "sd"}


def norm_name(s: object) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").replace("-", " ").strip()


def identity_tokens(s: object) -> frozenset[str]:
    return frozenset(t for t in norm_name(s).split()
                     if t not in _GENERIC_TOKENS)


# Evidence-backed ESPN -> API-Football search bridges. Each entry exists
# because the plain search MEASURABLY failed or was ambiguous on 2026-07-29,
# and the archived probe records which. Never add one from intuition: an
# unverified bridge is exactly the guess `ambiguous_team` exists to prevent.
SEARCH_ALIASES = {
    "internazionale": "Inter",
    "bayern munich": "Bayern Munchen",
    "ajax amsterdam": "Ajax",
    "feyenoord rotterdam": "Feyenoord",
    "stade rennais": "Rennes",
    "paris saint germain": "Paris Saint Germain",
    "borussia dortmund": "Borussia Dortmund",
}


def match_tier(query: str, candidate: str) -> str | None:
    """'exact' | 'token_set' | None. Substring containment is NOT a tier:
    it is what attached a reserve side to a senior club's book with full
    confidence on the friendlies surface (review P0-1), and B/academy teams
    make that a live hazard rather than a corner case."""
    q, c = norm_name(query), norm_name(candidate)
    if not q or not c:
        return None
    if q == c:
        return "exact"
    if identity_tokens(query) and identity_tokens(query) == identity_tokens(candidate):
        return "token_set"
    return None


# --- the client ------------------------------------------------------------

class Client:
    """One budgeted, delayed, redacting GET path. No tight retries.

    Backs off a full provider window on 429 (the per-minute limit resets in
    60s, so a shorter wait just burns another request), and aborts outright
    when the provider reports zero daily requests remaining — the quota is
    shared, and hammering a spent one is somebody else's outage."""

    def __init__(self, key: str | None = None, budget: int | None = None):
        self._key = key or load_key()
        self._base = config.APIFOOTBALL_BASE.rstrip("/")
        self._delay = config.APIFOOTBALL_REQUEST_DELAY_SECONDS
        self._budget = (budget if budget is not None
                        else config.APIFOOTBALL_REQUEST_BUDGET)
        self._timeout = config.APIFOOTBALL_TIMEOUT_SECONDS
        self._backoff = config.APIFOOTBALL_BACKOFF_SECONDS
        self._retries = config.APIFOOTBALL_MAX_RETRIES
        self._session = requests.Session()
        self.used = 0
        self.rate_limit: dict = {}

    def get(self, path: str, params: dict) -> dict:
        if self.used >= self._budget:
            raise BudgetExceeded(
                f"request budget {self._budget} reached before {path} — "
                "aborting rather than spending more of the day's quota")
        attempt = 0
        while True:
            if self.used:
                time.sleep(self._delay)
            self.used += 1
            try:
                r = self._session.get(
                    f"{self._base}/{path.lstrip('/')}", params=params,
                    headers={"x-apisports-key": self._key},
                    timeout=self._timeout)
            except requests.RequestException as exc:
                msg = _redact(f"{type(exc).__name__}: {exc}", self._key)
                if attempt >= self._retries:
                    raise ApiFootballError(f"{path}: {msg}") from None
                attempt += 1
                time.sleep(self._backoff)
                continue
            self.rate_limit = {
                "remaining_minute": r.headers.get("x-ratelimit-remaining"),
                "remaining_day": r.headers.get(
                    "x-ratelimit-requests-remaining"),
                "limit_day": r.headers.get("x-ratelimit-requests-limit"),
            }
            rem = self.rate_limit["remaining_day"]
            if rem is not None and str(rem).strip() in {"0", "0.0"}:
                raise BudgetExceeded(
                    "provider reports 0 daily requests remaining — aborting")
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt >= self._retries:
                    raise ApiFootballError(
                        f"{path}: HTTP {r.status_code} after "
                        f"{attempt + 1} attempts")
                attempt += 1
                time.sleep(self._backoff)
                continue
            try:
                body = r.json()
            except ValueError:
                raise ApiFootballError(
                    f"{path}: unparseable body "
                    f"{_redact(r.text[:300], self._key)}") from None
            errs = provider_errors(body)
            if errs:
                raise ApiFootballError(
                    f"{path}: provider errors "
                    f"{_redact('; '.join(errs), self._key)}")
            return body


def _refuse_mls(league_id: int) -> None:
    if int(league_id) == MLS_LEAGUE_ID:
        raise ApiFootballError(
            "MLS (league 253) is served by Sportec, not API-Football. MLS xG "
            "already exists free with a measured effect and sits inside the "
            "MLS engine signature; sourcing it here would invalidate the live "
            "approval for no gain. Refusing by name.")


# --- coverage measurement --------------------------------------------------

def measure_coverage(league_id: int, season: int, client: Client | None = None,
                     samples: int | None = None,
                     league_name: str | None = None,
                     country: str | None = None) -> dict:
    """Empirically measure whether this (league, season) carries usable xG.

    Samples completed fixtures SPREAD ACROSS the season and requires a numeric
    value for BOTH teams. Returns the measurement; `store_coverage` persists
    it. Never trusts the statistic type — see the module docstring."""
    _refuse_mls(league_id)
    c = client or Client()
    n = samples if samples is not None else config.APIFOOTBALL_COVERAGE_SAMPLES
    fb = c.get("fixtures", {"league": league_id, "season": season})
    done = completed_fixtures(fb)
    picks = spread_sample(done, n)
    rows = []
    for f in picks:
        sb = c.get("fixtures/statistics",
                   {"fixture": f["provider_fixture_id"]})
        teams = read_fixture_stats(sb)
        rows.append({
            "provider_fixture_id": f["provider_fixture_id"],
            "kickoff": (f["kickoff_iso"] or "")[:10],
            "round_label": f["round_label"],
            "teams_in_stats": len(teams),
            "type_present": sum(1 for t in teams if t["xg_type"]),
            "value_present": sum(1 for t in teams if t["xg"] is not None),
            "both_teams_have_value": (len(teams) == 2 and all(
                t["xg"] is not None for t in teams)),
        })
    hits = [r for r in rows if r["both_teams_have_value"]]
    if not rows:
        verdict, rate = XG_INDETERMINATE, None
    elif len(hits) == len(rows):
        verdict, rate = XG_AVAILABLE, 1.0
    elif hits:
        verdict, rate = XG_PARTIAL, round(len(hits) / len(rows), 3)
    else:
        verdict, rate = XG_ABSENT, 0.0
    return {
        "league_id": league_id, "season": season,
        "league_name": league_name, "country": country,
        "verdict": verdict, "measured_rate": rate,
        "samples_taken": len(rows), "samples_with_xg": len(hits),
        "completed_visible": len(done),
        # the hollow form, recorded as a fact rather than inferred
        "type_without_value_seen": any(
            r["type_present"] > 0 and r["value_present"] == 0 for r in rows),
        "miss_rounds": [r["round_label"] for r in rows
                        if not r["both_teams_have_value"]],
        "sampling_method": "spread_across_season",
        "measured_at": _now().isoformat(),
        "samples": rows,
    }


def store_coverage(m: dict) -> dict:
    """UPSERT one coverage measurement. Coverage is a current measurement of
    the provider, not an evidence trail, so this one IS an upsert — unlike
    the xG values, which are append-only."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    try:
        row = (s.query(ApiFootballLeagueCoverage)
               .filter_by(league_id=m["league_id"],
                          season=m["season"]).first())
        created = row is None
        if created:
            row = ApiFootballLeagueCoverage(league_id=m["league_id"],
                                            season=m["season"])
            s.add(row)
        row.league_name = m.get("league_name")
        row.country = m.get("country")
        row.verdict = m["verdict"]
        row.measured_rate = m["measured_rate"]
        row.samples_taken = m["samples_taken"]
        row.samples_with_xg = m["samples_with_xg"]
        row.type_without_value_seen = bool(m["type_without_value_seen"])
        row.miss_rounds_json = json.dumps(m.get("miss_rounds") or [])
        row.sampling_method = m.get("sampling_method")
        row.measured_at = _now()
        s.commit()
        return {"created": created, "verdict": m["verdict"],
                "league_id": m["league_id"], "season": m["season"]}
    finally:
        s.close()


def coverage_table() -> list[dict]:
    """Every measured (league, season) verdict. This IS the coverage
    documentation for this provider — nothing published exists to check it
    against, which is why the measurement date ships with every row."""
    if not plane_ready():
        return []
    s = get_session()
    try:
        out = []
        for r in (s.query(ApiFootballLeagueCoverage)
                  .order_by(ApiFootballLeagueCoverage.league_id).all()):
            out.append({
                "league_id": r.league_id, "season": r.season,
                "league_name": r.league_name, "country": r.country,
                "verdict": r.verdict, "measured_rate": r.measured_rate,
                "samples_taken": r.samples_taken,
                "samples_with_xg": r.samples_with_xg,
                "type_without_value_seen": bool(r.type_without_value_seen),
                "miss_rounds": json.loads(r.miss_rounds_json or "[]"),
                "sampling_method": r.sampling_method,
                "measured_at": (r.measured_at.isoformat()
                                if r.measured_at else None),
                "fittable": r.verdict in FITTABLE_VERDICTS,
            })
        return out
    finally:
        s.close()


def coverage_for(league_id: int, season: int | None = None) -> dict | None:
    if not plane_ready():
        return None
    s = get_session()
    try:
        q = s.query(ApiFootballLeagueCoverage).filter_by(league_id=league_id)
        if season is not None:
            q = q.filter_by(season=season)
        r = q.order_by(ApiFootballLeagueCoverage.season.desc()).first()
        if r is None:
            return None
        return {"league_id": r.league_id, "season": r.season,
                "league_name": r.league_name, "verdict": r.verdict,
                "measured_rate": r.measured_rate,
                "samples_taken": r.samples_taken,
                "measured_at": (r.measured_at.isoformat()
                                if r.measured_at else None),
                "fittable": r.verdict in FITTABLE_VERDICTS}
    finally:
        s.close()


# --- xG ingestion (append-only) -------------------------------------------

def _insert_xg(s, league_id, season, f, side, own, opp, read_at) -> str:
    """Insert one (fixture, side) reading unless an identical one exists.

    -> 'written' | 'unchanged' | 'corrected'. 'corrected' is the interesting
    one: the same (fixture, side) already holds a DIFFERENT value, so this is
    a retroactive provider change and BOTH readings are now on the record."""
    vh = value_hash(own["xg_raw"], opp["xg_raw"],
                    f["home_goals"] if side == "home" else f["away_goals"],
                    f["away_goals"] if side == "home" else f["home_goals"])
    existing = (s.query(ApiFootballFixtureXg)
                .filter_by(provider_fixture_id=f["provider_fixture_id"],
                           side=side).all())
    if any(e.value_hash == vh for e in existing):
        return "unchanged"
    kickoff = None
    if f.get("kickoff_iso"):
        try:
            kickoff = datetime.fromisoformat(
                str(f["kickoff_iso"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            kickoff = None
    s.add(ApiFootballFixtureXg(
        provider_fixture_id=f["provider_fixture_id"], side=side,
        league_id=league_id, season=season,
        provider_team_id=own["provider_team_id"],
        team_name=(own["team_name"] or "")[:96] or None,
        opponent_team_id=opp["provider_team_id"],
        xg=own["xg"],
        xg_raw=(None if own["xg_raw"] is None else str(own["xg_raw"])[:32]),
        xg_against=opp["xg"],
        goals=(f["home_goals"] if side == "home" else f["away_goals"]),
        goals_conceded=(f["away_goals"] if side == "home"
                        else f["home_goals"]),
        kickoff_utc=kickoff,
        round_label=(f.get("round_label") or "")[:64] or None,
        value_hash=vh, read_at=read_at))
    return "corrected" if existing else "written"


def ingest_league_xg(league_id: int, season: int,
                     client: Client | None = None,
                     max_fixtures: int | None = None,
                     skip_existing: bool = True) -> dict:
    """Ingest per-fixture team xG for one (league, season).

    `skip_existing` avoids the network for a (fixture, side) pair already
    held, which is what makes a resumed or repeated ingest cheap — the whole
    point given a shared 7,500/day quota. It is also why a deliberate
    re-read (to look for retroactive correction) must pass
    skip_existing=False: the cheap path cannot detect a change it never
    fetches, and pretending otherwise would be the silent-overwrite failure
    wearing a different hat."""
    _refuse_mls(league_id)
    if not plane_ready():
        return {"skipped": "dormant"}
    c = client or Client()
    fb = c.get("fixtures", {"league": league_id, "season": season})
    fixtures = completed_fixtures(fb)
    if max_fixtures:
        fixtures = fixtures[-max_fixtures:]      # most recent N, recency-first
    read_at = _now()
    counts = {"written": 0, "unchanged": 0, "corrected": 0,
              "skipped_existing": 0, "no_stats": 0, "one_sided": 0,
              "fetch_failed": 0}
    s = get_session()
    try:
        for f in fixtures:
            fid = f["provider_fixture_id"]
            if skip_existing:
                have = (s.query(ApiFootballFixtureXg)
                        .filter_by(provider_fixture_id=fid).count())
                if have >= 2:
                    counts["skipped_existing"] += 1
                    continue
            try:
                sb = c.get("fixtures/statistics", {"fixture": fid})
            except BudgetExceeded:
                s.commit()
                counts["aborted"] = "request budget reached"
                break
            except ApiFootballError:
                counts["fetch_failed"] += 1
                continue
            teams = read_fixture_stats(sb)
            if len(teams) != 2:
                counts["no_stats"] += 1
                continue
            by_id = {t["provider_team_id"]: t for t in teams}
            home = by_id.get(f["home_team_id"])
            away = by_id.get(f["away_team_id"])
            if home is None or away is None:
                # the provider's own team ids did not line up with its own
                # fixture — never guessed by position
                counts["one_sided"] += 1
                continue
            try:
                for side, own, opp in (("home", home, away),
                                       ("away", away, home)):
                    counts[_insert_xg(s, league_id, season, f, side,
                                      own, opp, read_at)] += 1
                s.commit()
            except Exception as exc:              # isolate one bad fixture
                s.rollback()
                counts["fetch_failed"] += 1
                counts.setdefault("errors", []).append(
                    {"fixture": fid, "error": str(exc)[:160]})
        return {"league_id": league_id, "season": season,
                "completed_fixtures": len(fixtures),
                "requests_used": c.used,
                "rate_limit": c.rate_limit,
                "counts": counts,
                "read_at": read_at.isoformat()}
    finally:
        s.close()


def retroactive_corrections(league_id: int | None = None) -> dict:
    """(fixture, side) pairs holding MORE THAN ONE distinct reading.

    Zero is the expected answer and a non-zero one is a finding, not an
    error: it means the provider changed a historical value under us. The
    append-only store is what makes this question answerable at all."""
    if not plane_ready():
        return {"dormant": True}
    from sqlalchemy import func
    s = get_session()
    try:
        q = (s.query(ApiFootballFixtureXg.provider_fixture_id,
                     ApiFootballFixtureXg.side,
                     func.count(ApiFootballFixtureXg.id).label("n"))
             .group_by(ApiFootballFixtureXg.provider_fixture_id,
                       ApiFootballFixtureXg.side)
             .having(func.count(ApiFootballFixtureXg.id) > 1))
        if league_id is not None:
            q = q.filter(ApiFootballFixtureXg.league_id == league_id)
        rows = [{"provider_fixture_id": r[0], "side": r[1], "readings": r[2]}
                for r in q.all()]
        return {"pairs_with_multiple_readings": len(rows), "detail": rows[:50]}
    finally:
        s.close()


def ingest_summary() -> dict:
    """How much xG this store actually holds, per (league, season) — the
    answer to 'is there enough to rate anything?', with the usable count
    stated separately from the row count so a league full of null values
    cannot read as covered."""
    if not plane_ready():
        return {"dormant": True}
    from sqlalchemy import distinct, func
    s = get_session()
    try:
        out = []
        pairs = (s.query(ApiFootballFixtureXg.league_id,
                         ApiFootballFixtureXg.season)
                 .distinct().all())
        for lid, season in pairs:
            base = s.query(ApiFootballFixtureXg).filter_by(
                league_id=lid, season=season)
            fixtures = (s.query(func.count(distinct(
                ApiFootballFixtureXg.provider_fixture_id)))
                .filter_by(league_id=lid, season=season).scalar() or 0)
            with_xg = (s.query(func.count(distinct(
                ApiFootballFixtureXg.provider_fixture_id)))
                .filter_by(league_id=lid, season=season)
                .filter(ApiFootballFixtureXg.xg.isnot(None)).scalar() or 0)
            out.append({
                "league_id": lid, "season": season,
                "rows": base.count(),
                "fixtures": fixtures,
                "fixtures_with_usable_xg": with_xg,
                "teams": (s.query(func.count(distinct(
                    ApiFootballFixtureXg.provider_team_id)))
                    .filter_by(league_id=lid, season=season).scalar() or 0),
            })
        out.sort(key=lambda r: (r["league_id"], r["season"]))
        return {"leagues": out}
    finally:
        s.close()


def league_xg_rows(league_id: int, season: int | None = None) -> list[dict]:
    """The LATEST reading per (fixture, side) for one league, for the fit.

    'Latest' matters because the store is append-only: where a provider
    correction produced two readings, the fit uses the most recent one and
    `retroactive_corrections` is what surfaces that it happened. Silently
    averaging two readings would blend a superseded value into a current
    rating."""
    if not plane_ready():
        return []
    s = get_session()
    try:
        q = s.query(ApiFootballFixtureXg).filter_by(league_id=league_id)
        if season is not None:
            q = q.filter_by(season=season)
        latest: dict[tuple, ApiFootballFixtureXg] = {}
        for r in q.all():
            key = (r.provider_fixture_id, r.side)
            cur = latest.get(key)
            if cur is None or (r.read_at and cur.read_at
                               and r.read_at > cur.read_at) \
                    or (cur.read_at is None and r.read_at is not None):
                latest[key] = r
        return [{
            "provider_fixture_id": r.provider_fixture_id, "side": r.side,
            "league_id": r.league_id, "season": r.season,
            "provider_team_id": r.provider_team_id, "team_name": r.team_name,
            "xg": r.xg, "xg_against": r.xg_against,
            "goals": r.goals, "goals_conceded": r.goals_conceded,
            "kickoff_utc": r.kickoff_utc, "round_label": r.round_label,
            "read_at": r.read_at,
        } for r in latest.values()]
    finally:
        s.close()


# --- club -> domestic league resolution -----------------------------------

def _domestic_leagues(payload: object, season: int | None) -> list[dict]:
    """League rows of type 'League' (a domestic competition) from a
    `/leagues?team=` response. Cups and continental competitions are excluded:
    a club's xG FORM comes from the league it plays in, not from six cup
    ties, and pooling them would mix populations."""
    out = []
    for row in response_items(payload):
        if not isinstance(row, dict):
            continue
        lg = row.get("league") or {}
        if str(lg.get("type") or "").lower() != "league":
            continue
        seasons = [s for s in (row.get("seasons") or [])
                   if isinstance(s, dict)]
        years = sorted(s.get("year") for s in seasons
                       if isinstance(s.get("year"), int))
        if not years:
            continue
        pick = season if (season in years) else years[-1]
        out.append({"league_id": lg.get("id"), "league_name": lg.get("name"),
                    "league_country": (row.get("country") or {}).get("name"),
                    "season": pick, "seasons_visible": years})
    return out


def resolve_club_league(query_name: str, client: Client | None = None,
                        season: int | None = None) -> dict:
    """Which domestic league does this club play in? Provider-derived.

    Two calls: `/teams?search=` for the club, then `/leagues?team=` for its
    competitions. Refuses rather than guesses:
      resolved          exactly one club matched at the deciding tier AND
                        exactly one domestic league came back
      ambiguous_team    more than one club matched at the deciding tier
      ambiguous_league  the club plays in more than one domestic league
      unresolved        nothing matched
    A near-miss name is NOT a match: only normalized equality or token-set
    equality counts, and substring containment is banned outright (it is what
    attached a reserve side to a senior club elsewhere on this surface)."""
    c = client or Client()
    search = SEARCH_ALIASES.get(norm_name(query_name), query_name)
    tb = c.get("teams", {"search": search})
    cands = []
    for row in response_items(tb):
        if not isinstance(row, dict):
            continue
        t = row.get("team") or {}
        tier = match_tier(query_name, t.get("name"))
        if tier is None and search != query_name:
            tier = match_tier(search, t.get("name"))
        cands.append({"provider_team_id": t.get("id"), "name": t.get("name"),
                      "country": (row.get("country")
                                  or (t.get("country"))),
                      "tier": tier})
    base = {"query_name": query_name, "searched_as": search,
            "candidates": cands, "resolved_at": _now().isoformat()}
    exact = [x for x in cands if x["tier"] == "exact"]
    tokens = [x for x in cands if x["tier"] == "token_set"]
    deciding = exact or tokens
    if not deciding:
        return {**base, "resolution": "unresolved"}
    if len(deciding) > 1:
        return {**base, "resolution": "ambiguous_team"}
    club = deciding[0]
    lb = c.get("leagues", {"team": club["provider_team_id"],
                           "current": "true"})
    leagues = _domestic_leagues(lb, season)
    out = {**base, "provider_team_id": club["provider_team_id"],
           "provider_team_name": club["name"], "country": club["country"],
           "match_tier": club["tier"], "leagues": leagues}
    if not leagues:
        return {**out, "resolution": "unresolved"}
    if len(leagues) > 1:
        return {**out, "resolution": "ambiguous_league"}
    lg = leagues[0]
    return {**out, "resolution": "resolved", "league_id": lg["league_id"],
            "league_name": lg["league_name"],
            "league_country": lg["league_country"], "season": lg["season"]}


def store_club_league(r: dict) -> dict:
    """Archive one club->league resolution, ambiguous and unresolved cases
    included. Storing the refusals is the point: it is what stops the same
    unresolvable club being re-probed against the quota every request, and it
    keeps the reason in words."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    try:
        row = (s.query(ApiFootballClubLeague)
               .filter_by(query_name=r["query_name"][:96]).first())
        created = row is None
        if created:
            row = ApiFootballClubLeague(query_name=r["query_name"][:96])
            s.add(row)
        row.resolution = r["resolution"]
        row.provider_team_id = r.get("provider_team_id")
        row.provider_team_name = (r.get("provider_team_name") or "")[:96] or None
        row.country = (r.get("country") or "")[:64] or None
        row.league_id = r.get("league_id")
        row.season = r.get("season")
        row.league_name = (r.get("league_name") or "")[:96] or None
        row.league_country = (r.get("league_country") or "")[:64] or None
        row.match_tier = r.get("match_tier")
        row.candidates_json = json.dumps(
            {"searched_as": r.get("searched_as"),
             "candidates": r.get("candidates") or [],
             "leagues": r.get("leagues") or []})[:60000]
        row.resolved_at = _now()
        s.commit()
        return {"created": created, "resolution": r["resolution"]}
    finally:
        s.close()


def club_league(query_name: str) -> dict | None:
    """The archived resolution for one club name, or None if never probed.
    None means 'not looked up', which the surface must say in those words —
    it is not 'this club has no league'."""
    if not plane_ready():
        return None
    s = get_session()
    try:
        r = (s.query(ApiFootballClubLeague)
             .filter_by(query_name=(query_name or "")[:96]).first())
        if r is None:
            return None
        return {"query_name": r.query_name, "resolution": r.resolution,
                "provider_team_id": r.provider_team_id,
                "provider_team_name": r.provider_team_name,
                "country": r.country, "league_id": r.league_id,
                "season": r.season, "league_name": r.league_name,
                "league_country": r.league_country,
                "match_tier": r.match_tier,
                "resolved_at": (r.resolved_at.isoformat()
                                if r.resolved_at else None)}
    finally:
        s.close()


def club_league_table() -> list[dict]:
    if not plane_ready():
        return []
    s = get_session()
    try:
        return [{"query_name": r.query_name, "resolution": r.resolution,
                 "provider_team_id": r.provider_team_id,
                 "league_id": r.league_id, "season": r.season,
                 "league_name": r.league_name,
                 "league_country": r.league_country,
                 "match_tier": r.match_tier}
                for r in (s.query(ApiFootballClubLeague)
                          .order_by(ApiFootballClubLeague.query_name).all())]
    finally:
        s.close()
