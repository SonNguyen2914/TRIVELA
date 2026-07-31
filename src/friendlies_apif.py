"""API-Football fixture backbone for the club-friendlies viewer.

WHY THIS MODULE EXISTS. `src/friendlies.py` sources fixtures from ESPN's
`club.friendly` scoreboard. Measured on 2026-07-29 against the live paid
PRO key, over the ET dates Kalshi was actually trading:

    Kalshi KXCLUBFGAME events with >=1 tradeable market      25
    ESPN club.friendly fixtures naming one of those 25        1
    API-Football fixtures naming one of those 25             22

ESPN could name exactly ONE tradeable friendly (Liverpool vs Wrexham).
The page therefore spoke about 4% of the market Son trades. That is a
COVERAGE defect, not a matching defect, and no amount of alias work on
the ESPN side could have fixed it — the fixtures were never in the
payload.

THE ROOT CAUSE WAS NOT "667 INSTEAD OF ESPN". A league-scoped sweep of
API-Football league 667 "Friendlies Clubs" is ALSO structurally blind,
and the way it is blind is the interesting part:

    Liverpool vs Wrexham   -> f1589333, league 1022
                              "Premier League - Summer Series", SEASON 2025
    Leeds Utd vs Sunderland-> f1589334, league 1022, SEASON 2025

Branded pre-season tournaments get their own league id AND their own
season number. `/fixtures?league=667&season=2026&from=&to=` cannot see
either fixture, and `/fixtures?team=40&season=2026` returns an empty
array for Liverpool while returning the fixture under `season=2025`.
An empty array from a season-scoped query is exactly the shape that has
burned this repo before (`src/live_feed.py:221`, `results: 0` on a
fixture live at the 45th minute), so it is never read as "no match on".

THE SWEEP IS THEREFORE DATE-SCOPED, NOT LEAGUE-SCOPED. `/fixtures?date=`
requires no `season` parameter and spans every league in one request per
UTC date. Measured: 4 requests, 1611 fixtures, and both league-1022
fixtures present. The full pool is the BRIDGE CANDIDATE POOL, so a Kalshi
event can bridge to a competition this module has never heard of — which
is precisely how league 1022 was found, and the mechanism means we can
never again be blind for the reason we were blind here. When a bridge
lands outside `FRIENDLY_LEAGUES` the census reports it as a discovery,
by name, instead of dropping it.

IDENTITY IS AN INTEGER, NEVER A NAME. An independent review raised a HIGH
finding that name+date matching is not provider-stable, and `AGENTS.md`
§5/§13 make that binding. So the bridge is
`normalized kalshi title side -> api-football team id`, curated from
archived evidence. Measured in the same window: 3020 team appearances
carried 0 ids with two different names, and 0 unordered id-pairs
collided on any ET date — ids are a sound join key and names are not.

Name matching may PROPOSE a bridge. Only a recorded id CONFIRMS one:

    bridged     both sides in TEAM_BRIDGE, and exactly one fixture on
                that ET date whose {home_id, away_id} equals the bridged
                pair. Authoritative.
    proposed    a name tier found a single plausible fixture but at
                least one side has no recorded id. SHOWN as a curation
                candidate, never treated as a bridge, never priced.
    ambiguous   more than one fixture satisfies the join. Refused; both
                are named. Never picked.
    unbridged   no candidate at all.
    sweep_incomplete  a sweep request failed, so an absence claim is
                unsupportable — the same distinction `src/friendlies.py`
                draws between `unmapped` and `registry_incomplete`.

Two structural findings that the bridge encodes rather than papers over:

ORIENTATION DISAGREES AND IS RECORDED, NEVER SILENTLY SWAPPED. Kalshi
titles "Bournemouth vs Augsburg"; API-Football has f1583494 as
`FC Augsburg(170) v Bournemouth(35)`. Same match, opposite home side.
The id join is on an unordered pair so the bridge still lands, but
`orientation: "reversed"` ships with it. Kalshi's legs are keyed by team
code rather than by home/away, so the book is unaffected — but a future
consumer that reads home/away from the fixture must be able to see that
the two providers disagreed.

RESERVE SIDES ARE DIFFERENT CLUBS WITH DIFFERENT IDS, AND THIS IS THE
WHOLE ARGUMENT FOR IDS. Kalshi's "Real Madrid Castilla" is API-Football's
`Real Madrid II`, id 9575. Senior Real Madrid is 541. The two names share
no token beyond "real"/"madrid", so no name tier can bridge it at all —
while a laxer matcher would have bridged it to the SENIOR club's fixture
with full confidence. That is the exact hazard `src/friendlies.py`'s P0-1
rule was written for, and an integer settles it.

SCOPE IS UNCHANGED. This module adds a fixture source and a census. It
adds NO model, NO shadow plane, NO locks, NO database writes and NO
scheduler jobs, and none is planned — `src.friendlies.FRAMING` is served
verbatim beside everything here. It performs no order path and reads no
money setting. Its only writes are to an in-process cache.

Provider evidence, archived before this module was pushed:
  research_archive/friendlies_apif_coverage_2026-07-29.json
  research_archive/friendlies_apif_bridge_2026-07-29.json
  research_archive/friendlies_apif_misses_2026-07-29.json
"""
from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

APIF_BASE = os.getenv("APIFOOTBALL_BASE", "https://v3.football.api-sports.io")

# Horizon and cadence are config, per the plan's budget (see REQUEST_BUDGET).
APIF_HORIZON_DAYS = int(os.getenv("FRIENDLIES_APIF_HORIZON_DAYS", "3"))

# The sweep looks BACKWARD as well as forward, and this is not padding for
# its own sake. A Kalshi event stays tradeable while the match is in play
# and until it settles, so at 00:02 UTC the still-trading Liverpool v
# Wrexham book pointed at a fixture on Eastern Jul 29 — a date a
# forward-only window had already left behind. Measured live: the census
# correctly refused to call it absent and reported `outside_horizon`
# instead, which is the guard working and ALSO a real coverage hole,
# because "many friendlies right now" is exactly the in-play case. One
# extra request per sweep buys the whole of yesterday.
APIF_LOOKBACK_DAYS = int(os.getenv("FRIENDLIES_APIF_LOOKBACK_DAYS", "1"))
APIF_SWEEP_TTL = float(os.getenv("FRIENDLIES_APIF_SWEEP_TTL", "600"))
APIF_TIMEOUT = float(os.getenv("FRIENDLIES_APIF_TIMEOUT", "20"))
APIF_DELAY = float(os.getenv("FRIENDLIES_APIF_DELAY", "0.5"))

# Burst-throttle between Kalshi BOOK fetches. Not decorative: the first
# live run of `market_rows` fetched 22 books back to back and Kalshi
# answered one of them with a 429. The fail-closed path handled it
# correctly — that row came back `unavailable` rather than pretending the
# market was closed — but a throttle we control is better than a refusal
# we provoke, because the honest failure still cost real coverage.
KALSHI_BOOK_DELAY = float(os.getenv("FRIENDLIES_KALSHI_BOOK_DELAY", "0.15"))

# The key file route is preferred over an env var so the secret never
# crosses a shell history or an agent transcript. Mode 600 is ENFORCED,
# not advised: reading a group-readable secret would quietly normalise
# leaving it readable. Same contract as scripts/verify_apifootball.py.
APIF_KEY_FILE = Path.home() / ".apifootball_key"
REDACTED = "<REDACTED-APIFOOTBALL-KEY>"

# `API_FOOTBALL_KEY` (config.py) is DELIBERATELY not a fallback: it may
# still hold the free-tier key, and the free plan was measured
# season-blind for current seasons in this project. Measuring the wrong
# plan confidently is the failure this avoids.
APIF_KEY_ENV = "APIFOOTBALL_KEY"

# PRO plan, as documented in docs/APIFOOTBALL-TRIAL.md §5.
REQUEST_BUDGET = {"per_minute": 300, "per_day": 7500}

# Leagues whose fixtures ARE club friendlies. Each is evidence-backed:
# observed in the archived 2026-07-29 sweep carrying fixtures that Kalshi
# lists under KXCLUBFGAME. Never add one from intuition — an unclassified
# league is still reachable by the bridge (see the module docstring) and
# gets reported as a discovery, which is the honest way to grow this set.
FRIENDLY_LEAGUES = {
    667: "Friendlies Clubs",
    1022: "Premier League - Summer Series",
}

KALSHI_CLUBF_GAME = "KXCLUBFGAME"
_TICKER_DATE = re.compile(rf"{KALSHI_CLUBF_GAME}-(\d{{2}}[A-Z]{{3}}\d{{2}})")

# Generic club-form tokens, IDENTICAL to src.friendlies._GENERIC_TOKENS.
# Nothing identity-bearing belongs here: a probe run with "united",
# "real", "city", "town" and "sporting" added broke four bridges that had
# worked (Albacete/Real Madrid Castilla, Sporting CP/Nottingham,
# Stade Lavallois/US Granville, Monza/Aris), which is recorded in the
# archive as a negative control. Stripping identity is how a B side gets
# attached to a senior club.
_GENERIC_TOKENS = {"fc", "cf", "sc", "cd", "ud", "ac", "afc", "cfc",
                   "cp", "club"}

# ---------------------------------------------------------------------------
# THE BRIDGE. normalized Kalshi title side -> (api-football team id,
# the provider name observed, the archived fixture it was observed in).
#
# Machine-generated from the archived probes, not hand-transcribed: the
# 18 pairs a conservative name tier proposed over the date-scoped pool,
# plus eight sides attributed individually because no name tier reaches
# them. Every entry names the fixture that is its evidence, so any entry
# can be re-verified against the committed archive.
#
# An entry is a claim that a Kalshi title side denotes a specific club.
# It is NOT a claim that the club has a fixture, and it is not a fixture
# mapping — the fixture is joined per-date at read time, so a stale entry
# cannot invent a match.
# ---------------------------------------------------------------------------
TEAM_BRIDGE: dict[str, tuple[int, str, str]] = {
    "al-ittihad": (2938, "Al-Ittihad FC", "f1598562"),
    "albacete": (722, "Albacete", "f1598568"),
    "andorra": (8157, "FC Andorra", "f1583496"),
    "arezzo": (876, "Arezzo", "f1583495"),
    "aris thessaloniki": (1123, "Aris Thessalonikis", "f1585001"),
    "augsburg": (170, "FC Augsburg", "f1583494"),
    "barcelona": (529, "Barcelona", "f1583514"),
    "birmingham": (54, "Birmingham", "f1583514"),
    "bournemouth": (35, "Bournemouth", "f1583494"),
    "bromley": (1832, "Bromley", "f1583511"),
    "caen": (88, "Caen", "f1604711"),
    "concarneau": (1300, "Concarneau", "f1548928"),
    "cordoba": (713, "Cordoba", "f1604708"),
    "crawley": (1362, "Crawley Town", "f1583512"),
    "derby": (69, "Derby", "f1583509"),
    "dieppe": (9274, "Dieppe", "f1604711"),
    "elche": (797, "Elche", "f1604708"),
    "europa": (593, "Europa Fc", "f1583496"),
    "folgore caratese": (9450, "Folgore Caratese", "f1583505"),
    "juventus": (496, "Juventus", "f1585004"),
    # APIF's name is the bare "Leeds"; Kalshi's is "Leeds United". The
    # token sets differ, so this resolves at the `loose` tier only —
    # which is a PROPOSAL, and the recorded id is what confirms it.
    "leeds united": (63, "Leeds", "f1589334"),
    "liverpool": (40, "Liverpool", "f1589333"),
    "mainz": (164, "FSV Mainz 05", "f1604714"),
    "malaga": (535, "Malaga", "f1598562"),
    "mansfield": (1374, "Mansfield Town", "f1583509"),
    "monza": (1579, "Monza", "f1585001"),
    "nice": (84, "Nice", "f1585004"),
    "nijmegen": (413, "NEC Nijmegen", "f1583504"),
    "nottingham": (65, "Nottingham Forest", "f1583500"),
    "parma calcio": (523, "Parma", "f1583495"),
    "pontivy gsi": (3098, "Pontivy GSI", "f1548928"),
    "qpr": (72, "QPR", "f1583511"),
    "reading": (53, "Reading", "f1583512"),
    # THE RESERVE-SIDE CASE. 9575 is Real Madrid II. Senior Real Madrid
    # is 541 and must never be substituted here — see the module
    # docstring. No name tier can reach this pair, by design.
    "real madrid castilla": (9575, "Real Madrid II", "f1598568"),
    "sassuolo": (488, "Sassuolo", "f1583505"),
    "sevilla": (536, "Sevilla", "f1583504"),
    "shabab al-ahli": (2870, "Shabab Al Ahli Dubai", "f1604714"),
    "southport": (1826, "Southport", "f1583515"),
    "sporting cp": (228, "Sporting CP", "f1583500"),
    "stade lavallois": (433, "Laval", "f1604699"),
    "sunderland": (746, "Sunderland", "f1589334"),
    "us granville": (3185, "Granville", "f1604699"),
    "wigan": (61, "Wigan", "f1583515"),
    "wrexham": (1837, "Wrexham", "f1589333"),
}

# Kalshi title sides measured on 2026-07-29 to be UNBRIDGEABLE because
# API-Football has no fixture for the club in the traded window — a
# provider absence, not a naming gap. Recorded by name so the residual is
# never reported as a percentage, and so a later curation pass does not
# re-derive the finding. Each carries what WAS found, which is the part
# that matters: two of these are cases where a name matcher would have
# bridged to the WRONG club.
KNOWN_PROVIDER_ABSENCES = {
    "fc rottach-egern": (
        "no such team in API-Football (/teams?search=Rottach -> 0 rows). "
        "Bayern Munchen exists as id 157 but has NO fixture in the traded "
        "window under season 2026 or 2025. The three `bayern` fixtures in "
        "the window are Bayern Hof (12737), Bayern Munchen II (4674) and "
        "Bayern Alzenau (9361) — none is the senior side, so a matcher "
        "that stripped `II` would have bridged to the reserve team."),
    "bayern munich": (
        "id 157 confirmed, no fixture in the traded window in ANY league "
        "(season 2026 and season 2025 both empty). Not a "
        "Munich/Munchen normalisation gap: normalisation was never the "
        "obstacle, the fixture is not in the provider."),
    "luanda": (
        "unidentifiable: /teams?search=Luanda returns Petro de Luanda "
        "(4922), Benfica Luanda (11149), Luanda Villa (22230, Kenya) and "
        "Luanda City (24909), and zero of them appears anywhere in the "
        "traded window. Guessing between four clubs is the thing this "
        "module refuses to do."),
    "teruel": (
        "id 5278 confirmed, no fixture in the traded window in any "
        "league under season 2026 or 2025."),
    "essen": (
        "Kalshi's own leg code is SWE, not RWE, which points at "
        "Schwarz-Weiss Essen — a club API-Football's team search does "
        "not carry at all. The only Essen side in the window is "
        "Rot-Weiss Essen (1621), playing Bayer Leverkusen on a DIFFERENT "
        "ET date. Matching on the name `Essen` would have bridged to the "
        "wrong club; refusing is the correct behaviour here."),
    "ennepetal": (
        "id 14666 confirmed, no fixture in the traded window in any "
        "league under season 2026 or 2025."),
}

FRAMING_NOTE = (
    "Fixtures come from API-Football; books come from Kalshi. Fixtures "
    "are joined to books by API-Football team id, never by name — an "
    "unbridged side is reported as unbridged and is never guessed. No "
    "model runs on friendlies and none is planned.")

# ---------------------------------------------------------------------------
# cache — this module's own dict, never shared (a shared dict cross-serves
# leagues on identical keys, a trap src/friendlies.py already documents)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}
_flight_locks: dict[str, threading.Lock] = {}
_flight_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _flight_guard:
        return _flight_locks.setdefault(key, threading.Lock())


def reset_cache() -> None:
    """Drop every cached sweep. Tests only — there is no runtime caller."""
    _cache.clear()


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

def load_key() -> tuple[str, str, list[str]]:
    """(key, where_it_came_from, problems). Never logs the key itself."""
    problems: list[str] = []
    env = (os.getenv(APIF_KEY_ENV) or "").strip()
    if env:
        return env, APIF_KEY_ENV, problems
    if not APIF_KEY_FILE.exists():
        return "", "nowhere", problems
    try:
        mode = APIF_KEY_FILE.stat().st_mode & 0o777
    except OSError as exc:
        problems.append(f"could not stat {APIF_KEY_FILE.name}: {exc}")
        return "", APIF_KEY_FILE.name, problems
    if mode & 0o077:
        problems.append(
            f"{APIF_KEY_FILE} is mode {mode:03o}; it must be 600 "
            f"(owner-only). Refusing to read a group- or world-readable "
            f"secret. Fix with: chmod 600 {APIF_KEY_FILE}")
        return "", APIF_KEY_FILE.name, problems
    try:
        raw = APIF_KEY_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"could not read {APIF_KEY_FILE.name}: {exc}")
        return "", APIF_KEY_FILE.name, problems
    key = raw.strip()          # dashboard copy-paste smuggles newlines
    if not key:
        problems.append(f"{APIF_KEY_FILE.name} is empty")
    return key, APIF_KEY_FILE.name, problems


def redact(text: object, key: str) -> str:
    """Strip the key from anything bound for stdout or an archive. The key
    only ever travels in a header, but a provider error string or a
    requests exception can echo one back."""
    out = str(text)
    if key:
        out = out.replace(key, REDACTED)
        s = key.strip()
        if s and s != key:
            out = out.replace(s, REDACTED)
    return out


# ---------------------------------------------------------------------------
# payload primitives — pure
# ---------------------------------------------------------------------------

def response_items(payload: object) -> list:
    """The `response` ARRAY, which is the only thing that counts. The
    provider's own `results` field is context and is never trusted as the
    count: docs/APIFOOTBALL-TRIAL.md §2 is the governing rule, and the
    reason it exists is `results: 0` on a fixture that was live."""
    if not isinstance(payload, dict):
        return []
    r = payload.get("response")
    return r if isinstance(r, list) else []


def provider_errors(payload: object) -> list[str]:
    """API-Football reports plan/token/parameter problems INSIDE a 200
    body, as `errors` — a dict when populated, [] when not."""
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


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").strip()


def identity_tokens(s: str) -> frozenset[str]:
    return frozenset(t for t in norm_name(s).split()
                     if t not in _GENERIC_TOKENS)


def name_tier(kalshi_side: str, apif_name: str) -> str | None:
    """-> "exact" | "tokenset" | "loose" | None. A PROPOSAL tier only;
    nothing here confirms a bridge. Substring containment is banned, as
    in src.friendlies._side_identity: it is what attached a Castilla
    fixture to the senior club's book."""
    k, a = norm_name(kalshi_side), norm_name(apif_name)
    if not k or not a:
        return None
    if k == a:
        return "exact"
    kt, at = identity_tokens(kalshi_side), identity_tokens(apif_name)
    if kt and kt == at:
        return "tokenset"
    if kt and at and (kt <= at or at <= kt):
        return "loose"
    return None


def et_date_of_ticker(event_ticker: str) -> str | None:
    """KXCLUBFGAME-26JUL29LFCWRE -> '26JUL29'."""
    m = _TICKER_DATE.match(event_ticker or "")
    return m.group(1) if m else None


def et_date_of_iso(iso: str) -> str | None:
    """A UTC kickoff -> the Kalshi-style US-Eastern date segment. Real
    wall-clock Eastern via IANA zone, never a fixed offset (AGENTS.md
    §13)."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%y%b%d").upper()


def parse_fixture(row: object) -> dict | None:
    """One API-Football fixture -> the flat shape this surface serves.
    Returns None for a row missing the identity that makes it usable: a
    fixture id, both team ids, and a kickoff. A row we cannot identify is
    ABSENT, not a fixture with zeroes in it."""
    if not isinstance(row, dict):
        return None
    fx = row.get("fixture") or {}
    lg = row.get("league") or {}
    teams = row.get("teams") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}
    fid, hid, aid = fx.get("id"), home.get("id"), away.get("id")
    kickoff = fx.get("date")
    if not isinstance(fid, int) or not isinstance(hid, int) \
            or not isinstance(aid, int) or not kickoff:
        return None
    et = et_date_of_iso(kickoff)
    if et is None:
        return None
    goals = row.get("goals") or {}
    status = fx.get("status") or {}
    return {
        "fixture_id": fid,
        "kickoff_utc": kickoff,
        "et_date": et,
        "status": status.get("short"),
        "status_long": status.get("long"),
        "elapsed": status.get("elapsed"),
        "league_id": lg.get("id"),
        "league_name": lg.get("name"),
        "league_season": lg.get("season"),
        "is_classified_friendly": lg.get("id") in FRIENDLY_LEAGUES,
        # the league's country, when the provider gives one. Used ONLY to
        # disambiguate a club name across countries (Al-Sadd SC in Qatar
        # vs Al Sadd in Yemen); frequently "World" on a friendly, which
        # disambiguates nothing and correctly leaves the pair refused.
        "league_country": lg.get("country"),
        # the competition round, verbatim. Load-bearing for cup
        # competitions: a 2nd-qualifying-round tie and a league-stage match
        # are not the same kind of fixture, and flattening them hides that
        # most of a Conference League season IS qualifying.
        "round": lg.get("round"),
        # crest comes free in the same payload; venue is frequently
        # {id: None, name: None, city: None} on this feed, so it is
        # reported as ABSENT rather than as an empty string
        "venue": ((row.get("fixture") or {}).get("venue") or {}).get("name"),
        "venue_city": ((row.get("fixture") or {}).get("venue")
                       or {}).get("city"),
        "home": {"apif_team_id": hid, "name": home.get("name"),
                 "crest": home.get("logo")},
        "away": {"apif_team_id": aid, "name": away.get("name"),
                 "crest": away.get("logo")},
        # A null score is ABSENT, never 0-0. A fixture that has not
        # kicked off has no score, and rendering one as nil-nil would
        # invent a result.
        "goals": {"home": goals.get("home"), "away": goals.get("away")},
    }


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def utc_dates_for(horizon_days: int, today: datetime | None = None,
                  lookback_days: int | None = None) -> list[str]:
    """The UTC dates that must be fetched to cover `lookback_days` behind
    plus `horizon_days` of US-Eastern dates from today.

    ET date D spans UTC D 04:00 -> D+1 04:00, so covering N ET dates
    needs N+1 UTC dates. Dropping that trailing pad silently loses every
    late-evening Eastern kickoff, which on the measured slate would have
    been most of the Jul 31 card. The LEADING pad exists for in-play
    events — see APIF_LOOKBACK_DAYS."""
    n = max(1, min(int(horizon_days), 14))
    back = max(0, min(int(APIF_LOOKBACK_DAYS if lookback_days is None
                          else lookback_days), 7))
    base = (today or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(-back, n + 1)]


def covered_et_dates(utc_dates: list[str]) -> set[str]:
    """The US-Eastern dates a given set of UTC date buckets covers
    COMPLETELY.

    ET date E spans UTC E 04:00 -> E+1 04:00, so E is fully covered only
    when BOTH E and E+1 were fetched. The last UTC bucket in a sweep is
    therefore a partial tail and its ET date is NOT covered.

    This exists because of a defect caught in live verification: with a
    1-day horizon the census reported 17 events `unbridged` with the
    reason "API-Football lists no fixture pairing them on this date". It
    had never looked at those dates. Asserting an absence from a window
    that was never swept is exactly the "missing evidence rendered as
    authority" failure this surface is built to refuse — so an event
    outside the horizon gets its own state instead."""
    out: set[str] = set()
    for d in (utc_dates or [])[:-1]:
        try:
            out.add(datetime.strptime(d, "%Y-%m-%d")
                    .strftime("%y%b%d").upper())
        except (TypeError, ValueError):
            continue
    return out


def _fetch_date(session, key: str, utc_date: str) -> tuple[list[dict] | None, str | None]:
    """One UTC date's fixtures, or (None, reason). A 200 carrying an
    empty array WITH a provider error is a FAILURE, not an answer — the
    distinction the free plan's season-blindness taught this repo."""
    try:
        r = session.get(f"{APIF_BASE}/fixtures", params={"date": utc_date},
                        headers={"x-apisports-key": key},
                        timeout=APIF_TIMEOUT)
    except requests.RequestException as exc:
        return None, redact(f"{type(exc).__name__}: {exc}", key)
    try:
        body = r.json()
    except ValueError:
        return None, f"HTTP {r.status_code}: unparseable body"
    errs = provider_errors(body)
    if errs:
        return None, redact(f"HTTP {r.status_code}: provider errors "
                            f"{'; '.join(errs)}", key)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    rows = response_items(body)
    out = [p for p in (parse_fixture(x) for x in rows) if p is not None]
    return out, None


def sweep(horizon_days: int | None = None, today: datetime | None = None) -> dict:
    """Every fixture API-Football lists across the horizon, all leagues.

    -> {"fixtures", "utc_dates", "complete", "failures", "requests_used",
        "key_source", "problems", "cached"}

    `complete` is False when ANY date failed. The partial list is KEPT (a
    fixture found in it still bridges) but no absence claim may be made
    from it — the same rule src/friendlies.py applies to a partial Kalshi
    registry. An incomplete sweep is NEVER cached, so the next read
    retries.

    One request per UTC date. The defaults — 1 day of lookback plus a
    3-day horizon — are 5 requests; at the default 600s TTL that is at
    most 720 requests/day against a 7500/day plan (9.6%), and 5 per
    refresh against a 300/min limit."""
    days = APIF_HORIZON_DAYS if horizon_days is None else horizon_days
    dates = utc_dates_for(days, today)
    ckey = f"sweep:{','.join(dates)}"

    now = time.monotonic()
    hit = _cache.get(ckey)
    if hit and now - hit[0] < APIF_SWEEP_TTL:
        return {**hit[1], "cached": True}

    with _lock_for(ckey):
        hit = _cache.get(ckey)
        if hit and time.monotonic() - hit[0] < APIF_SWEEP_TTL:
            return {**hit[1], "cached": True}

        key, source, problems = load_key()
        if not key:
            # No key is NOT an empty fixture list. It is an inability to
            # look, and it must read that way all the way to the census.
            out = {"fixtures": [], "utc_dates": dates, "complete": False,
                   "failures": [{"utc_date": d, "reason": "no API-Football "
                                 "key available"} for d in dates],
                   "requests_used": 0, "key_source": source,
                   "problems": problems}
            return {**out, "cached": False}

        session = requests.Session()
        fixtures: list[dict] = []
        failures: list[dict] = []
        used = 0
        for i, d in enumerate(dates):
            if i:
                time.sleep(APIF_DELAY)       # burst-throttle, 300/min plan
            used += 1
            rows, reason = _fetch_date(session, key, d)
            if rows is None:
                failures.append({"utc_date": d, "reason": reason})
                continue
            fixtures.extend(rows)

        seen: set[int] = set()
        uniq = [f for f in fixtures
                if f["fixture_id"] not in seen
                and not seen.add(f["fixture_id"])]
        uniq.sort(key=lambda f: f["kickoff_utc"])
        out = {"fixtures": uniq, "utc_dates": dates,
               "complete": not failures, "failures": failures,
               "requests_used": used, "key_source": source,
               "problems": problems}
        if out["complete"]:
            _cache[ckey] = (time.monotonic(), out)
        return {**out, "cached": False}


def friendly_fixtures(swept: dict) -> list[dict]:
    """The subset of a sweep whose league is a CLASSIFIED friendly
    competition. The bridge deliberately searches the whole sweep instead
    (see the module docstring), so this is the display list, not the
    candidate pool."""
    return [f for f in swept.get("fixtures") or []
            if f["is_classified_friendly"]]


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------

def bridge_side(kalshi_side: str) -> dict:
    """One Kalshi title side -> its recorded API-Football team id, or an
    explicit refusal. Never guesses."""
    k = norm_name(kalshi_side)
    rec = TEAM_BRIDGE.get(k)
    if rec:
        tid, aname, evidence = rec
        return {"kalshi_side": kalshi_side, "normalized": k,
                "apif_team_id": tid, "apif_team_name": aname,
                "state": "recorded", "evidence": evidence, "note": None}
    known = KNOWN_PROVIDER_ABSENCES.get(k)
    if known:
        return {"kalshi_side": kalshi_side, "normalized": k,
                "apif_team_id": None, "apif_team_name": None,
                "state": "provider_absence", "evidence": None,
                "note": known}
    return {"kalshi_side": kalshi_side, "normalized": k,
            "apif_team_id": None, "apif_team_name": None,
            "state": "unrecorded", "evidence": None,
            "note": "no recorded API-Football team id for this Kalshi "
                    "title side; a name match may propose one but only a "
                    "recorded id confirms a bridge"}


def kalshi_sides(title: str) -> tuple[str, str] | None:
    """"A vs B" -> ("A", "B"). The KXCLUBFGAME grammar; anything else is
    not a two-sided match title and is refused rather than split."""
    if " vs " not in (title or ""):
        return None
    home, away = title.split(" vs ", 1)
    home, away = home.strip(), away.strip()
    return (home, away) if home and away else None


def _propose_by_name(sides, pool) -> list[dict]:
    """Name-tier candidates, orientation-tolerant. PROPOSALS ONLY."""
    kh, ka = sides
    out = []
    for f in pool:
        for orientation, (x, y) in (
                ("same", (f["home"], f["away"])),
                ("reversed", (f["away"], f["home"]))):
            t1 = name_tier(kh, x["name"] or "")
            t2 = name_tier(ka, y["name"] or "")
            if t1 and t2:
                out.append({"fixture": f, "orientation": orientation,
                            "tiers": [t1, t2]})
                break
    return out


def bridge_event(event: dict, swept: dict) -> dict:
    """One Kalshi KXCLUBFGAME event -> its API-Football fixture, or an
    explicit refusal.

    -> {"event_ticker", "title", "et_date", "state", "reason",
        "sides", "fixture", "orientation", "candidates", "proposal"}

    state is one of bridged / ambiguous / proposed / unbridged /
    outside_horizon / sweep_incomplete / not_a_match_title. `fixture` is
    populated ONLY for `bridged`; a proposal carries its candidate under
    `proposal` so a consumer cannot mistake one for the other by reading
    the same field.

    Three of those states are all ways of saying "we could not resolve
    this", and they are kept apart on purpose, because only ONE of them
    is a statement about the provider:
      unbridged        we looked and API-Football has no such fixture
      outside_horizon  we never looked — the date is beyond the sweep
      sweep_incomplete we tried to look and a request failed
    """
    ticker = event.get("event_ticker") or ""
    title = event.get("title") or ""
    etd = et_date_of_ticker(ticker)
    base = {"event_ticker": ticker, "title": title, "et_date": etd,
            "state": "unbridged", "reason": None, "sides": None,
            "fixture": None, "orientation": None, "candidates": [],
            "proposal": None}

    sides = kalshi_sides(title)
    if etd is None or sides is None:
        return {**base, "state": "not_a_match_title",
                "reason": "event ticker or title does not follow the "
                          "KXCLUBFGAME '{YYMONDD}' / 'A vs B' grammar"}

    h = bridge_side(sides[0])
    a = bridge_side(sides[1])
    base["sides"] = {"home": h, "away": a}

    # An event on a date the sweep never covered is NOT an absence. See
    # covered_et_dates for the live defect this closes.
    covered = covered_et_dates(swept.get("utc_dates") or [])
    if covered and etd not in covered:
        return {**base, "state": "outside_horizon",
                "reason": f"this event's Eastern date {etd} is outside the "
                          f"swept horizon "
                          f"({', '.join(sorted(covered))}), so no absence "
                          f"can be claimed — the fixtures were never "
                          f"requested. Raise FRIENDLIES_APIF_HORIZON_DAYS "
                          f"or the `days` parameter to reach it."}

    pool = [f for f in swept.get("fixtures") or [] if f["et_date"] == etd]
    complete = bool(swept.get("complete"))

    # --- confirmed path: both ids recorded, joined on the unordered pair
    if h["apif_team_id"] is not None and a["apif_team_id"] is not None:
        want = frozenset((h["apif_team_id"], a["apif_team_id"]))
        hits = [f for f in pool
                if frozenset((f["home"]["apif_team_id"],
                              f["away"]["apif_team_id"])) == want]
        if len(hits) > 1:
            # Measured 0 collisions across 1611 fixtures, but a
            # doubleheader or a provider duplicate must refuse, not pick.
            return {**base, "state": "ambiguous",
                    "reason": "more than one fixture on this ET date has "
                              "exactly this pair of API-Football team ids",
                    "candidates": [f["fixture_id"] for f in hits]}
        if len(hits) == 1:
            f = hits[0]
            orientation = ("same"
                           if f["home"]["apif_team_id"] == h["apif_team_id"]
                           else "reversed")
            return {**base, "state": "bridged", "fixture": f,
                    "orientation": orientation,
                    "candidates": [f["fixture_id"]]}
        if not complete:
            return {**base, "state": "sweep_incomplete",
                    "reason": "both team ids are recorded but no fixture "
                              "matched, and the sweep was incomplete — an "
                              "absence cannot be claimed from a partial "
                              "sweep"}
        return {**base, "reason": "both team ids are recorded but "
                                  "API-Football lists no fixture pairing "
                                  "them on this date"}

    # --- proposal path: at least one side has no recorded id
    props = _propose_by_name(sides, pool)
    if len(props) == 1:
        p = props[0]
        return {**base, "state": "proposed",
                "reason": "a name tier found a single plausible fixture, "
                          "but at least one side has no recorded "
                          "API-Football team id. A proposal is not a "
                          "bridge: it is shown for curation and is never "
                          "treated as resolved.",
                "orientation": p["orientation"],
                "candidates": [p["fixture"]["fixture_id"]],
                "proposal": {"fixture": p["fixture"],
                             "tiers": p["tiers"],
                             "orientation": p["orientation"]}}
    if len(props) > 1:
        return {**base, "state": "ambiguous",
                "reason": "more than one fixture matches by name and no "
                          "recorded id can decide between them",
                "candidates": [p["fixture"]["fixture_id"] for p in props]}
    if not complete:
        return {**base, "state": "sweep_incomplete",
                "reason": "no candidate found, but the sweep was "
                          "incomplete — an absence cannot be claimed "
                          "from a partial sweep"}
    unrec = [s["kalshi_side"] for s in (h, a) if s["state"] == "unrecorded"]
    absent = [s["kalshi_side"] for s in (h, a)
              if s["state"] == "provider_absence"]
    bits = []
    if absent:
        bits.append("measured provider absence for " + ", ".join(absent))
    if unrec:
        bits.append("no recorded team id for " + ", ".join(unrec))
    return {**base, "reason": "; ".join(bits) or "no candidate fixture"}


# ---------------------------------------------------------------------------
# coverage — denominated, always
# ---------------------------------------------------------------------------

def coverage(kalshi_events: list[dict], swept: dict | None = None,
             horizon_days: int | None = None) -> dict:
    """The census. Every count ships with its denominator and the
    unbridged events ship BY NAME — this repo's rule is that a count
    without its denominator is a defect, and "18 bridged" alone is not
    acceptable output.

    -> {"kalshi_open_events", "fixtures_known", "fixtures_known_all_leagues",
        "events_bridged", "events_unbridged", "unbridged", "bridged",
        "fraction_bridged", "breakdown", "league_discoveries",
        "sweep", "framing_note", ...}

    `fraction_bridged` is None, never 0.0, when there is nothing to
    divide by: an empty market is not 0% coverage, it is no market.
    """
    swept = sweep(horizon_days) if swept is None else swept
    rows = [bridge_event(ev, swept) for ev in kalshi_events or []]

    by_state: dict[str, list[dict]] = {}
    for r in rows:
        by_state.setdefault(r["state"], []).append(r)

    bridged = by_state.get("bridged", [])
    unbridged = [r for r in rows if r["state"] != "bridged"]
    total = len(rows)

    # A bridge landing outside FRIENDLY_LEAGUES is a DISCOVERY, not a
    # row to drop. This is how league 1022 was found; reporting it is how
    # the classified set grows from evidence instead of from intuition.
    discoveries: dict[int, dict] = {}
    for r in bridged:
        f = r["fixture"]
        if not f["is_classified_friendly"]:
            discoveries.setdefault(f["league_id"], {
                "league_id": f["league_id"], "league_name": f["league_name"],
                "league_season": f["league_season"], "events": []})
            discoveries[f["league_id"]]["events"].append(r["event_ticker"])

    fixtures_all = swept.get("fixtures") or []
    friendly = friendly_fixtures(swept)

    # "We never looked" is reported apart from "it is not there". Lumping
    # them would let a short horizon masquerade as provider absence — the
    # defect covered_et_dates was added to close.
    outside = by_state.get("outside_horizon", [])
    in_horizon = total - len(outside)

    return {
        # --- the denominators, first, so no count can be read alone ----
        "kalshi_open_events": total,
        "kalshi_open_events_in_horizon": in_horizon,
        "fixtures_known": len(friendly),
        "fixtures_known_all_leagues": len(fixtures_all),
        # --- the counts ------------------------------------------------
        "events_bridged": len(bridged),
        "events_unbridged": len(unbridged),
        "events_outside_horizon": len(outside),
        "fraction_bridged": (round(len(bridged) / total, 4)
                             if total else None),
        # The same count against the denominator it was actually measured
        # against. Both are reported because neither alone is honest: the
        # first understates the join, the second hides the horizon.
        "fraction_bridged_in_horizon": (round(len(bridged) / in_horizon, 4)
                                        if in_horizon else None),
        # --- and the names, because a residual is not an answer -------
        "bridged": [{"event_ticker": r["event_ticker"], "title": r["title"],
                     "et_date": r["et_date"],
                     "fixture_id": r["fixture"]["fixture_id"],
                     "league_id": r["fixture"]["league_id"],
                     "league_name": r["fixture"]["league_name"],
                     "orientation": r["orientation"]}
                    for r in bridged],
        "unbridged": [{"event_ticker": r["event_ticker"], "title": r["title"],
                       "et_date": r["et_date"], "state": r["state"],
                       "reason": r["reason"],
                       "candidates": r["candidates"]}
                      for r in unbridged],
        "breakdown": {s: len(v) for s, v in sorted(by_state.items())},
        "league_discoveries": sorted(discoveries.values(),
                                     key=lambda d: d["league_id"]),
        "sweep": {"utc_dates": swept.get("utc_dates"),
                  "complete": swept.get("complete"),
                  "failures": swept.get("failures"),
                  "requests_used": swept.get("requests_used"),
                  "cached": swept.get("cached"),
                  "key_source": swept.get("key_source"),
                  "problems": swept.get("problems"),
                  "horizon_days": (APIF_HORIZON_DAYS if horizon_days is None
                                   else horizon_days),
                  "classified_friendly_leagues": dict(
                      sorted(FRIENDLY_LEAGUES.items()))},
        "request_budget": dict(REQUEST_BUDGET),
        "framing_note": FRAMING_NOTE,
    }


# ---------------------------------------------------------------------------
# integration with the existing viewer
#
# `src/friendlies.py` is imported, never modified: its Kalshi fetchers and
# its fail-closed cache states are the parts that already encode the P0
# review lessons, and a second copy would fork those fixes. This module
# adds the fixture backbone and the census beside them.
# ---------------------------------------------------------------------------

def tradeable_events(max_pages: int = 3) -> dict:
    """The KXCLUBFGAME events that actually have a tradeable market, with
    completeness on the record: {"events", "listed_total", "complete",
    "truncated"}.

    Nested markets are requested so tradeable-ness costs no extra call —
    the census needs "how many events are OPEN", and per-event market
    fetches would be 25+ requests to answer a question two already
    answer. An incomplete registry is never cached and never supports an
    absence claim."""
    from src import friendlies

    key = "kalshi:tradeable"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 120.0:
        return {**hit[1], "cached": True}
    with _lock_for(key):
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < 120.0:
            return {**hit[1], "cached": True}
        events: list[dict] = []
        complete = True
        cursor = None
        for _ in range(max_pages):
            params: dict = {"series_ticker": KALSHI_CLUBF_GAME, "limit": 200,
                            "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            d = friendlies._get_json(f"{friendlies.KALSHI_BASE}/events", params)
            if d is None:
                complete = False
                cursor = None
                break
            events.extend(d.get("events") or [])
            cursor = d.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)                  # burst-throttle (Kalshi 429s)
        truncated = bool(cursor)
        open_events = [e for e in events
                       if any(m.get("status") in friendlies.TRADEABLE
                              for m in (e.get("markets") or []))]
        out = {"events": open_events, "listed_total": len(events),
               "complete": complete, "truncated": truncated}
        if complete and not truncated:
            _cache[key] = (time.monotonic(), out)
        return {**out, "cached": False}


def _book_for(row: dict) -> tuple[dict | None, dict | None]:
    """A bridged event's parsed 3-way book + its freshness. Delegates to
    src.friendlies so `unavailable` and `stale` keep their existing,
    distinct wording — a failed fetch is "we couldn't look", never "no
    open markets"."""
    from src import friendlies
    from src.mls import parse_game_books

    rows, meta = friendlies.event_markets_state(row["event_ticker"])
    if rows is None:
        return None, meta
    cand = {"event_ticker": row["event_ticker"], "title": row["title"]}
    book = parse_game_books([cand], {row["event_ticker"]: rows})[0]
    return book, (meta if meta["state"] == "stale" else None)


def market_rows(horizon_days: int | None = None) -> dict:
    """The API-Football-backed friendlies surface: every tradeable Kalshi
    event, each carrying its bridge state and — only when BRIDGED and
    only when the book could be read — its Kalshi book and the
    market-implied block.

    Every existing honesty invariant is preserved rather than restated:
      - `src.friendlies.FRAMING` is served verbatim; no model runs here.
      - an ambiguous or unresolved bridge renders NO price: `book` stays
        None for every state except `bridged`.
      - a failed book fetch is `book_state: "unavailable"`; a warm cache
        served through a failure carries `freshness.state == "stale"` with
        its age. The two are never collapsed.
      - no bare TAKE/BUY/SELL exists on this surface, and no field here
        is a recommendation.

    The market-implied block is looked up on `src.friendlies` at call
    time rather than imported at module scope. It lives on the
    `feat-friendlies-implied` branch, which is NOT merged to main as this
    is written; resolving it dynamically means this module is correct on
    both sides of that merge instead of importing a name that may not
    exist. When it is absent the row says so in words — `implied` is
    None and `implied_unavailable_reason` explains why — rather than
    quietly omitting the field."""
    from src import friendlies

    reg = tradeable_events()
    swept = sweep(horizon_days)
    cov = coverage(reg["events"], swept, horizon_days)

    implied_fn = getattr(friendlies, "market_implied", None)
    clock_fn = getattr(friendlies, "_read_clock", None)
    clock = clock_fn() if callable(clock_fn) else None

    rows = []
    fetched = 0
    for ev in reg["events"]:
        b = bridge_event(ev, swept)
        row = {
            "event_ticker": b["event_ticker"],
            "title": b["title"],
            "et_date": b["et_date"],
            "bridge_state": b["state"],
            "bridge_reason": b["reason"],
            "sides": b["sides"],
            "orientation": b["orientation"],
            "candidates": b["candidates"],
            "proposal": b["proposal"],
            "fixture": b["fixture"],
            "book": None,
            "book_state": None,
            "freshness": None,
            "implied": None,
            "implied_unavailable_reason": None,
        }
        if b["state"] != "bridged":
            # No price for an unresolved identity. The bridge_state and
            # bridge_reason already say why, in words.
            row["book_state"] = "not_bridged"
            row["implied_unavailable_reason"] = (
                "no price is rendered for an unbridged fixture")
            rows.append(row)
            continue
        if fetched:
            time.sleep(KALSHI_BOOK_DELAY)    # see KALSHI_BOOK_DELAY
        fetched += 1
        book, stale = _book_for(row)
        if book is None:
            row["book_state"] = "unavailable"
            row["freshness"] = stale
            row["implied_unavailable_reason"] = (
                "the Kalshi book could not be fetched — this is "
                "'we could not look', not 'no market'")
            rows.append(row)
            continue
        if not book.get("markets"):
            row["book_state"] = "no_open_markets"
            row["book"] = book
            row["freshness"] = stale
            row["implied_unavailable_reason"] = (
                "the book was fetched and holds nothing tradeable")
            rows.append(row)
            continue
        row["book_state"] = "open"
        row["book"] = book
        row["freshness"] = stale
        if callable(implied_fn):
            row["implied"] = implied_fn(book, stale, clock)
        else:
            row["implied_unavailable_reason"] = (
                "the market-implied block is not present in this build "
                "(src.friendlies.market_implied is absent)")
        rows.append(row)

    return {
        "rows": rows,
        "coverage": cov,
        "kalshi_registry": {"listed_total": reg["listed_total"],
                            "open_events": len(reg["events"]),
                            "complete": reg["complete"],
                            "truncated": reg["truncated"],
                            "cached": reg["cached"]},
        "fixtures": friendly_fixtures(swept),
        "framing": friendlies.FRAMING,
        "framing_note": FRAMING_NOTE,
    }


# --- fixture-primary surface (the friendlies HUB, 2026-07-30) --------------
# `market_rows` above is Kalshi-event-primary: it answers "what can I trade
# and did it bridge". That is the wrong shape for a hub, which has to show
# EVERY friendly whether or not Kalshi lists it. Measured 2026-07-29: ESPN's
# club.friendly bucket could name 1 of 25 tradeable events (4%); this
# date-scoped all-leagues sweep bridges 22 of 25 (88%) and, more to the
# point, sees the hundreds of friendlies Kalshi never lists at all.


def _bridge_by_fixture_id(swept: dict, events: list[dict]) -> dict:
    """{apif fixture id -> bridge row} for every BRIDGED Kalshi event.

    Factored out of `market_rows`'s loop so the fixture-primary and
    event-primary surfaces share one bridge computation instead of two that
    could disagree about the same fixture."""
    out: dict[int, dict] = {}
    for ev in events:
        b = bridge_event(ev, swept)
        if b.get("state") != "bridged":
            continue
        # the id is NESTED under "fixture", not flat on the row — reading
        # b["fixture_id"] silently yielded None for every event, so the hub
        # showed "no kalshi book" on all 399 rows while 20 of 22 tradeable
        # events were in fact bridging correctly.
        fid = ((b.get("fixture") or {}).get("fixture_id"))
        if fid is not None:
            # carry the event itself: the caller needs its priced legs to
            # put the market on the same scale as the read, and re-fetching
            # it per fixture would spend the provider budget on data we
            # already hold.
            b = dict(b, event=ev)
            out[int(fid)] = b
    return out


# Statuses that mean the match is OVER (or will never happen). A fixture
# list that keeps these is not a fixture list — measured 2026-07-30, the hub
# was carrying 84 dead rows, 81 of them the previous day's, which is what
# made a 399-row table unusable.
FINISHED_STATUSES = {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "WO", "PST"}


def fixture_rows(horizon_days: int | None = None,
                 with_strength: bool = True,
                 include_finished: bool = False) -> dict:
    """Every classified friendly in the horizon, Kalshi-listed or not.

    UPCOMING ONLY by default. The sweep is date-scoped and necessarily
    includes the current UTC day's earlier hours (and the previous day, for
    late kickoffs in western timezones), so without this filter yesterday's
    finished matches sit at the TOP of a chronological list. Pass
    include_finished=True for the raw view.

    Deliberately does NOT fetch a book per fixture: only the handful that
    bridge to a Kalshi event are worth a request, and the hub renders
    hundreds of rows. `kalshi.state` says which are worth opening.
    """
    swept = sweep(horizon_days)
    fixtures = friendly_fixtures(swept)
    reg: dict = {}
    try:
        reg = tradeable_events()
        bridged = _bridge_by_fixture_id(swept, reg["events"])
        reg_ok = True
    except Exception as exc:                      # never lose the fixtures
        bridged, reg_ok = {}, False
        print(f"[friendlies-apif] tradeable events failed: {exc}")

    rows = []
    for f in fixtures:
        fid = f.get("fixture_id")
        b = bridged.get(int(fid)) if fid is not None else None
        row = dict(f)
        row["kalshi"] = (
            {"state": "bridged", "event_ticker": b.get("event_ticker")}
            if b else
            {"state": "none_listed" if reg_ok else "registry_unavailable",
             "means": ("no Kalshi event bridges to this fixture"
                       if reg_ok else
                       "the Kalshi registry could not be read, so whether "
                       "this fixture is listed is UNKNOWN — not 'unlisted'")})
        if with_strength:
            row["strength"] = _slim_strength(_strength_for(f))
            # The market beside the read, for the ~20 fixtures that bridge.
            # Uses the event ALREADY fetched for the bridge — no extra
            # request, so the list stays inside the provider budget.
            if b and b.get("event"):
                try:
                    from src.live import match_pick
                    import config as _cfg
                    row["market_vs_read"] = match_pick.compare(
                        row["strength"], b["event"],
                        (f.get("home") or {}).get("name") or "",
                        (f.get("away") or {}).get("name") or "",
                        draw_rate=_cfg.FRIENDLY_DRAW_RATE)
                except Exception as exc:
                    print(f"[friendlies-apif] compare {fid}: {exc}")
        rows.append(row)
    rows.sort(key=lambda r: (r.get("kickoff_utc") or ""))

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    finished = [r for r in rows
                if (r.get("status") in FINISHED_STATUSES
                    or (r.get("kickoff_utc") or "") < now_iso)]
    if not include_finished:
        fin_ids = {id(r) for r in finished}
        rows = [r for r in rows if id(r) not in fin_ids]

    # Tradeable Kalshi events that bridged to NO fixture. Surfaced rather
    # than dropped: this list is the answer to "show me everything the venue
    # is actually pricing", and silently omitting an event because our own
    # fixture feed missed it would make the hub quietly less complete than
    # the market it is watching.
    bridged_tickers = {b.get("event_ticker") for b in bridged.values()}
    kalshi_only = []
    if reg_ok:
        for ev in (reg.get("events") or []):
            tk = ev.get("event_ticker")
            if tk and tk not in bridged_tickers:
                kalshi_only.append({
                    "event_ticker": tk, "title": ev.get("title"),
                    "state": "no_fixture_match",
                    "means": ("this market is tradeable but no API-Football "
                              "fixture bridged to it, so it has no teams, "
                              "no kickoff and no strength read here")})

    return {"fixtures": rows, "count": len(rows),
            # said once, not 304 times — see _STRENGTH_CONSTANTS
            "strength_notes": strength_notes() if with_strength else None,
            "finished_hidden": len(finished),
            "include_finished": include_finished,
            "kalshi_only": kalshi_only,
            "kalshi_tradeable_total": (len(reg.get("events") or [])
                                       if reg_ok else None),
            "kalshi_listed_total": reg.get("listed_total") if reg_ok else None,
            "kalshi_counts_note": (
                "listed_total counts every event the series carries, most of "
                "them SETTLED past matches; tradeable_total is the subset "
                "with an open market. Only the second is a number of things "
                "you could bet on now."),
            "sweep": {"dates": swept.get("dates"),
                      "complete": swept.get("complete"),
                      "fixtures_seen": len(swept.get("fixtures") or [])},
            "kalshi_registry_read": reg_ok,
            "coverage_note": (
                "fixtures come from a DATE-scoped sweep across every league, "
                "not from a friendlies-league query: branded pre-season "
                "tournaments get their own league id and are invisible to a "
                "league-scoped call (measured, see module docstring)")}


def fixture_by_id(fixture_id: int, horizon_days: int | None = None) -> dict | None:
    """One friendly, from the sweep cache when possible, else one request."""
    swept = sweep(horizon_days)
    for f in friendly_fixtures(swept):
        if int(f.get("fixture_id") or -1) == int(fixture_id):
            return f
    # outside the cached horizon: one targeted request, using this
    # module's own key/redaction idiom rather than a second client
    key, _source, _problems = load_key()
    if not key:
        return None
    try:
        r = requests.get(f"{APIF_BASE}/fixtures",
                         params={"id": str(int(fixture_id))},
                         headers={"x-apisports-key": key},
                         timeout=APIF_TIMEOUT)
        if r.status_code != 200:
            return None
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[friendlies-apif] fixture {fixture_id}: "
              f"{redact(str(exc), key)[:160]}")
        return None
    for row in response_items(body):
        p = parse_fixture(row)
        if p and int(p.get("fixture_id") or -1) == int(fixture_id):
            return p
    return None


# Constants that are byte-identical on EVERY row. Measured 2026-07-30: the
# list served 0.7 MB for 304 fixtures, of which 0.5 MB was these four
# strings repeated 304 times — 1,568 bytes of strength block against 706
# bytes of actual fixture data per row. Through Vercel's proxy that took
# 77 SECONDS while the backend itself answered in 0.20s, so the friendlies
# page rendered no rows at all in production.
#
# They are not dropped: `strength_notes` carries them ONCE at the top
# level, so nothing that was said before goes unsaid.
_STRENGTH_CONSTANTS = ("estimate_meaning", "semantics",
                       "home_field_advantage", "attribution")


def _slim_strength(s: dict) -> dict:
    """The per-row strength read WITHOUT the boilerplate constants."""
    return {k: v for k, v in (s or {}).items()
            if k not in _STRENGTH_CONSTANTS}


def strength_notes() -> dict:
    """The constants stripped from every row, served once."""
    try:
        from src.live import club_strength_estimate as cse
        return {"estimate_class": cse.ESTIMATE_CLASS,
                "estimate_meaning": cse.ESTIMATE_MEANING}
    except Exception:
        return {}


def _strength_for(f: dict) -> dict:
    """The cross-league strength read, or a NAMED reason there is none.

    Kept behind a lazy import and a broad except on purpose: this is
    reader context attached to a fixture list, and a provider wobble in it
    must never cost the caller the fixtures themselves."""
    try:
        from src.live import club_strength_estimate as cse
        return cse.for_fixture(f)
    except Exception as exc:
        return {"available": False, "reason": "estimate_unavailable",
                "reason_words": "the strength read could not be computed",
                "detail": str(exc)[:160]}
