#!/usr/bin/env python3
"""Pre-registered verification harness for API-Football (api-sports.io).

    APIFOOTBALL_KEY=... python scripts/verify_apifootball.py

DECIDES ONE THING: is a paid API-Football key worth keeping past the one
month Son bought? Four checks, each pass/fail, all four fixed in this file
BEFORE any key existed — see docs/APIFOOTBALL-TRIAL.md. The criteria are
hashed (CRITERIA_FINGERPRINT) so that editing a threshold is visible in the
diff instead of silently retuning the answer to whatever the data turned
out to look like. That tamper-evidence is the whole point of the file.

================================ GOVERNING RULE ================================
ASSERT A NON-ZERO COUNT, NEVER A STATUS CODE.
A 200 WITH NOTHING IN IT IS A FAILURE, AND THIS HARNESS SAYS SO IN THOSE
WORDS.
================================================================================

Why that rule is written in capitals at the top of a script: it is the
failure this repository has paid for more than any other.

  - src/live_feed.py:221 records API-Football answering `results: 0` for a
    season-2026 fixture that was live at the 45th minute. HTTP 200. The
    free plan was season-blind and the empty array read as "no match on".
  - A `{"created": 0}`-shaped success hid every paper fill for a night
    while a full production volume silently rejected each write.

So no check here is allowed to conclude anything from `r.status_code`, from
`errors: []`, or from the provider's own `results` field. Every check
counts the actual elements of `response` and compares the count with what
the provider claimed. Where the two disagree, the count wins and the
disagreement is printed.

The one deliberate exception is CHECK 4, which tests for the ABSENCE of a
value. There, an empty payload is the desired result. It is called out
where it happens so nobody reads it as the rule being relaxed.

EXIT CODES
    0  KEEP           all four checks passed
    1  DROP           at least one check failed
    2  INDETERMINATE  the harness could not measure (no key, budget abort,
                      transport failure). Not a verdict: missing evidence
                      is never converted into confidence.
    3  DROP-CRITICAL  pre-match xG was populated, i.e. an xG feature could
                      carry the outcome. Disqualifying on its own.

SECRETS
    The key comes from APIFOOTBALL_KEY, or from ~/.apifootball_key if that
    variable is unset — the same shape as this repo's ~/.wc26_admin_token,
    so the key never has to cross a shell history or an agent transcript.
    That file must be mode 600; a group- or world-readable one is refused
    rather than used.

    Either way the key travels only in a request header. It is never
    printed, never stored, never placed in a URL or a param, and every byte
    written to the archive or to stdout passes through _redact() first.

    The repo's older API_FOOTBALL_KEY variable is deliberately NOT a
    fallback: it may still hold the free-tier key, and the free tier was
    measured season-blind, so reading it would produce a confident answer
    about the wrong plan.

BUDGET
    PRO is 300 requests/minute and 7,500/day. This run is capped at
    CRITERIA["request_budget"]["hard_cap"] requests with a fixed delay
    between them, and retries at most once, after a fixed sleep, never in
    a tight loop. The count used is printed and archived.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = Path(__file__).resolve().parent / "apifootball_espn_roster.json"
ARCHIVE_DIR = REPO_ROOT / "research_archive"
REDACTED = "<APIFOOTBALL_KEY_REDACTED>"

# --------------------------------------------------------------------------
# PRE-REGISTERED CRITERIA — fixed 2026-07-29, before a key existed.
#
# Nothing below may be edited to accommodate a result. CRITERIA_FINGERPRINT
# is asserted at startup, so a changed threshold halts the harness until the
# fingerprint literal is updated too — which puts the change in the diff.
# --------------------------------------------------------------------------
CRITERIA = {
    "version": "1.0",
    "registered_utc": "2026-07-29",
    "registered_before_any_key_existed": True,
    "governing_rule": (
        "Assert a non-zero COUNT, never a status code. An HTTP 200 carrying "
        "an empty or zeroed payload is a FAILURE."
    ),
    "check1_xg_presence": {
        "required_leagues": ["epl", "laliga", "ligamx", "mls"],
        "distinct_teams_required_per_fixture": 2,
        "xg_present_means": "non-null, numerically parseable, strictly > 0",
        "rejected_values": ["null", "empty string", "0", "0.0", "-", "non-numeric"],
        "statistic_type_accept_exact": ["xg"],
        "statistic_type_accept_substring": "expectedgoals",
        "statistic_type_reject_substrings": [
            "against", "conceded", "prevented", "allowed", "opponent", "xga",
        ],
        "completed_fixture_rule": (
            "the most recent fixture with status FT/AET/PEN whose kickoff is at "
            "least 24h in the past (so stats have had time to be posted); if no "
            "such fixture exists, the most recent FT/AET/PEN fixture, flagged"
        ),
        "season_fallback_rule": (
            "if the current season has no completed fixture, fall back ONCE to "
            "the most recent earlier season the plan lists, and print which "
            "season was used"
        ),
        "pass_rule": "all four leagues yield xG > 0 for BOTH teams",
    },
    "check2_current_season": {
        "season_derivation": (
            "calendar-derived, not provider-declared: split_year leagues -> "
            "year if month >= 7 else year - 1; calendar_year leagues -> year"
        ),
        "why_not_provider_declared": (
            "the free tier's season list is plan-filtered, so a plan-blind "
            "season could flag itself 'current' and pass a check it fails"
        ),
        "pass_rule": "len(response) > 0 for every one of the four leagues",
        "http_200_with_empty_response_array_is": "A FAILURE",
        "provider_results_field": "recorded and cross-checked, never trusted",
    },
    "check3_identity": {
        "team_id_rule": (
            "one team, two different fixtures, fetched as two separate "
            "/fixtures?id= calls -> the API-Football team id must be identical"
        ),
        "player_id_rule": (
            "one player present by name in both fixtures' /fixtures/players -> "
            "identical player id. No player shared between the two fixtures is "
            "INDETERMINATE, which counts as FAIL"
        ),
        "resolution_tiers": [
            "exact_normalised", "frozen_alias", "token_set_equal",
            "unique_containment",
        ],
        "generic_tokens_dropped": ["fc", "afc", "cf", "sc", "club"],
        "initialism_rule": (
            "POST-REGISTRATION AMENDMENT A1, 2026-07-29, recorded in "
            "docs/APIFOOTBALL-TRIAL.md §7: a run of consecutive "
            "single-character tokens collapses into one token, so 'D.C. "
            "United' -> 'dc united' and 'U.N.A.M. - Pumas' -> 'unam pumas'. "
            "This fixes a defect in OUR normaliser, not a threshold. It does "
            "not change the 100%/75% lines or the zero-ambiguity rule, and it "
            "does not turn the 2026-07-29 pre-registered FAIL into a PASS "
            "retroactively"
        ),
        "ambiguity_rule": (
            "any API-Football team resolving to more than one ESPN name, or two "
            "API-Football teams claiming the same ESPN name, FAILS EXPLICITLY "
            "and is named. An ambiguous identity match must never silently pick "
            "a team"
        ),
        "min_resolution_rate": {
            "mls": 1.0, "epl": 0.75, "laliga": 0.75, "ligamx": 0.75,
        },
        "min_resolution_rate_rationale": (
            "MLS's ESPN reference is operator-curated and slate-verified, so it "
            "is held to 100%. The eng.1 / esp.1 / mex.1 ESPN /teams payloads "
            "were MEASURED on 2026-07-29 to carry clubs that are not in those "
            "top flights and to omit clubs that are, which can account for "
            "roughly 20-25% of a roster by itself. 0.75 is the line below which "
            "the failure can no longer be blamed on our own stale reference. "
            "Both directions are always printed by name so a human attributes "
            "each miss rather than reading a percentage"
        ),
        "pass_rule": (
            "team id stable AND player id stable AND every league at or above "
            "its threshold AND zero ambiguous resolutions anywhere"
        ),
    },
    "check4_pre_match_leakage": {
        "fixture_rule": (
            "status.short == 'NS' AND the kickoff timestamp is strictly in the "
            "future — the status alone is a provider claim"
        ),
        "critical_fail_rule": (
            "any xG statistic present with a non-null value other than 0 on a "
            "not-started fixture is a CRITICAL FAIL: an xG feature could "
            "silently carry the outcome, which this repo treats as a "
            "disqualifying defect"
        ),
        "observation_rule": (
            "a null or exactly-zero pre-match xG placeholder carries no outcome "
            "information; it is reported, not failed"
        ),
        "empty_response_is": (
            "PASS — the single check where an absent payload is the desired "
            "result, stated separately from the counts rule on purpose"
        ),
        "no_not_started_fixture_found": (
            "INDETERMINATE, which counts as FAIL: a leakage check that could "
            "not run must never read as clean"
        ),
    },
    "overall": {
        "KEEP": "all four checks PASS",
        "DROP": "anything else, naming the failing check",
    },
    "request_budget": {
        "plan_limits": "300 requests/minute, 7500/day (PRO)",
        "hard_cap": 40,
        "delay_seconds": 0.5,
        "max_retries_per_request": 1,
        "retry_delay_seconds": 5.0,
        "timeout_seconds": 20,
    },
}

CRITERIA_FINGERPRINT = "7b7106b0342d2c5d951aa98196432ddb054d2a9c1492fa7277468b537eb79ba8"

# The frozen ESPN reference roster is the OTHER half of the instrument, and it
# lives in a separate file that CRITERIA_FINGERPRINT does not cover. That gap
# was exploited within an hour of the harness first running: two aliases were
# added to the roster — precisely and only the two team names that had just
# failed CHECK 3 — which turned a pre-registered DROP into a KEEP under an
# UNCHANGED criteria fingerprint, and the pre-registered archive was
# overwritten in place. See
# research_archive/apifootball_tuning_incident_2026-07-29.json.
#
# So the roster now carries the same tamper-evidence the thresholds do: its
# sha256 is asserted against this literal, and editing the roster halts the
# harness until the literal is updated too — which puts the edit in the diff.
ROSTER_FINGERPRINT = "1c0f01745bcb72ff8621abdb9cf6420b0180f8400bff631817dbb15b26ab8a3c"

FINISHED_STATUSES = {"FT", "AET", "PEN"}
NOT_STARTED_STATUS = "NS"
GENERIC_TOKENS = frozenset(CRITERIA["check3_identity"]["generic_tokens_dropped"])

PASS, FAIL, CRITICAL_FAIL, INDETERMINATE = (
    "PASS", "FAIL", "CRITICAL_FAIL", "INDETERMINATE")
# INDETERMINATE never reads as clean: it is folded into FAIL at verdict time.
FAILING = {FAIL, CRITICAL_FAIL, INDETERMINATE}


def criteria_fingerprint(criteria: dict | None = None) -> str:
    """sha256 over the canonical form of the pre-registered criteria."""
    blob = json.dumps(criteria if criteria is not None else CRITERIA,
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def roster_fingerprint(path: Path | None = None) -> str:
    """sha256 of the frozen ESPN reference file, exactly as committed."""
    return hashlib.sha256((path or ROSTER_PATH).read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------

KEY_FILE = Path.home() / ".apifootball_key"


def load_key() -> tuple[str, str, list[str]]:
    """(key, where_it_came_from, problems).

    APIFOOTBALL_KEY wins. Otherwise ~/.apifootball_key, which must be mode
    600 — a world-readable secret file is refused, not used, because using
    it would quietly normalise leaving the key readable.
    """
    problems: list[str] = []
    env = os.getenv("APIFOOTBALL_KEY", "").strip()
    if env:
        return env, "APIFOOTBALL_KEY", problems
    if not KEY_FILE.exists():
        return "", "nowhere", problems
    try:
        mode = KEY_FILE.stat().st_mode & 0o777
    except OSError as exc:
        problems.append(f"could not stat {KEY_FILE.name}: {exc}")
        return "", str(KEY_FILE.name), problems
    if mode & 0o077:
        problems.append(
            f"{KEY_FILE} is mode {mode:03o}; it must be 600 (owner-only). "
            f"Refusing to read a group- or world-readable secret. Fix with: "
            f"chmod 600 {KEY_FILE}")
        return "", str(KEY_FILE.name), problems
    try:
        raw = KEY_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"could not read {KEY_FILE.name}: {exc}")
        return "", str(KEY_FILE.name), problems
    # .strip() on purpose: dashboard copy-paste smuggles trailing newlines
    # into secrets, and a whitespace-damaged one already failed silently once
    # in this repo — the ntfy push-topic setting, 2026-07-22.
    key = raw.strip()
    if not key:
        problems.append(f"{KEY_FILE.name} is empty")
    return key, str(KEY_FILE.name), problems


def _redact(text: str, key: str) -> str:
    """Strip the key from anything on its way to stdout or to disk.

    Belt and braces: the key is only ever sent as a header and is never
    placed in a path, a param or a log line, but a provider error string or
    a requests exception could still echo one back.
    """
    out = str(text)
    if key:
        out = out.replace(key, REDACTED)
        # tolerate whitespace-damaged copies of the same secret
        stripped = key.strip()
        if stripped and stripped != key:
            out = out.replace(stripped, REDACTED)
    return out


# --------------------------------------------------------------------------
# payload primitives — pure, unit-tested against recorded payloads
# --------------------------------------------------------------------------

def errors_of(payload: object) -> list[str]:
    """API-Football reports plan/token/parameter problems INSIDE a 200 body,
    as `errors`, which is a dict when populated and [] when not."""
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
    """The elements of `response`, or [] — never None, never a bare truthiness
    test on the payload."""
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response")
    return resp if isinstance(resp, list) else []


def declared_results(payload: object) -> object:
    return payload.get("results") if isinstance(payload, dict) else None


def count_disagreement(payload: object) -> str | None:
    """The provider's own `results` field versus the length of the array it
    shipped. `results: 0` beside a populated array (or the reverse) is the
    exact shape of the 2026-07-09 incident, so it is always reported."""
    declared, actual = declared_results(payload), len(response_items(payload))
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    if declared != actual:
        return (f"provider 'results' field says {declared} but the response "
                f"array holds {actual} — the count wins")
    return None


def norm_stat_type(t: object) -> str:
    return "".join(c for c in str(t or "").lower() if c.isalnum())


def is_xg_type(t: object) -> bool:
    """Is this statistic type an expected-goals FOR figure?

    Rejects the against/conceded/prevented variants: xGA is a different
    statistic and accepting it would let 'xG coverage' be claimed on the
    strength of the opponent's number.
    """
    n = norm_stat_type(t)
    if not n:
        return False
    c = CRITERIA["check1_xg_presence"]
    if any(bad in n for bad in c["statistic_type_reject_substrings"]):
        return False
    return n in set(c["statistic_type_accept_exact"]) or \
        c["statistic_type_accept_substring"] in n


def xg_number(raw: object) -> float | None:
    """Parse an xG value, or None. None/''/'-'/'null'/non-numeric are all
    ABSENCE, not zero — the whole failure mode being guarded against is a
    hollow value read as an answer."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if not s or s.lower() in {"-", "–", "—", "null", "none", "n/a", "na"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def xg_findings(stats_payload: object) -> dict:
    """Read /fixtures/statistics and report, per team, whether an xG figure
    is genuinely present — plus every statistic type the provider offered, so
    'the type is simply not there' can be stated as a fact rather than
    inferred from a missing value."""
    teams, types_union = [], []
    any_xg_type = False
    for entry in response_items(stats_payload):
        if not isinstance(entry, dict):
            continue
        team = entry.get("team") or {}
        stats = entry.get("statistics")
        stats = stats if isinstance(stats, list) else []
        raw, value, seen_type = None, None, None
        for st in stats:
            if not isinstance(st, dict):
                continue
            t = st.get("type")
            if t is not None and str(t) not in types_union:
                types_union.append(str(t))
            if seen_type is None and is_xg_type(t):
                seen_type, raw = str(t), st.get("value")
                value = xg_number(raw)
                any_xg_type = True
        teams.append({
            "team_id": team.get("id"),
            "team_name": team.get("name"),
            "xg_statistic_type": seen_type,
            "xg_raw": raw,
            "xg_value": value,
            "xg_present": value is not None and value > 0,
            "statistics_count": len(stats),
        })
    return {
        "teams": teams,
        "xg_statistic_type_seen_anywhere": any_xg_type,
        "statistic_types_offered": types_union,
    }


def _result(status: str, measurements: dict, reasons=None, notes=None) -> dict:
    return {"status": status, "measurements": measurements,
            "reasons": list(reasons or []), "notes": list(notes or [])}


# --------------------------------------------------------------------------
# CHECK 1 — xG exists for all four leagues
# --------------------------------------------------------------------------

def check1_league(league_key: str, season: object, fixture_id: object,
                  stats_payload: object) -> dict:
    m = {"league": league_key, "season": season, "fixture_id": fixture_id,
         "provider_errors": errors_of(stats_payload),
         "provider_results_field": declared_results(stats_payload),
         "team_entries": len(response_items(stats_payload))}
    reasons, notes = [], []
    if m["provider_errors"]:
        notes.append(f"provider returned errors inside a 200: {m['provider_errors']}")
    dis = count_disagreement(stats_payload)
    if dis:
        notes.append(dis)

    if not response_items(stats_payload):
        reasons.append(
            "HTTP 200 WITH AN EMPTY response ARRAY — THIS IS A FAILURE, NOT A "
            "SUCCESS: no team statistics were returned for a completed fixture")
        m.update(xg_statistic_type_seen=False, statistic_types_offered=[], teams=[])
        return _result(FAIL, m, reasons, notes)

    f = xg_findings(stats_payload)
    m["teams"] = f["teams"]
    m["xg_statistic_type_seen"] = f["xg_statistic_type_seen_anywhere"]
    m["statistic_types_offered"] = f["statistic_types_offered"]

    ids = {t["team_id"] for t in f["teams"] if t["team_id"] is not None}
    need = CRITERIA["check1_xg_presence"]["distinct_teams_required_per_fixture"]
    if len(ids) < need:
        reasons.append(f"expected {need} distinct teams in the response, "
                       f"found {len(ids)} — cannot claim per-team coverage")

    if not f["xg_statistic_type_seen_anywhere"]:
        offered = ", ".join(f["statistic_types_offered"][:40]) or "(none at all)"
        reasons.append(
            "THE EXPECTED-GOALS STATISTIC TYPE IS ABSENT FROM THE RESPONSE "
            "ENTIRELY — not null, not zero: the provider does not offer it for "
            f"this fixture. Statistic types it did offer: {offered}")
    else:
        for t in f["teams"]:
            if t["xg_present"]:
                continue
            if t["xg_value"] is None:
                reasons.append(
                    f"{t['team_name']!r}: xG type {t['xg_statistic_type']!r} is "
                    f"present but its value is empty/null/non-numeric "
                    f"({t['xg_raw']!r}) — absence, not zero")
            else:
                reasons.append(
                    f"{t['team_name']!r}: xG is {t['xg_value']} — a zero is not "
                    "presence")

    return _result(FAIL if reasons else PASS, m, reasons, notes)


# --------------------------------------------------------------------------
# CHECK 2 — current-season visibility
# --------------------------------------------------------------------------

def check2_league(league_key: str, season: object, payload: object) -> dict:
    count = len(response_items(payload))
    m = {"league": league_key, "season_requested": season,
         "fixtures_counted": count,
         "provider_results_field": declared_results(payload),
         "provider_errors": errors_of(payload)}
    reasons, notes = [], []
    if m["provider_errors"]:
        notes.append(f"provider returned errors inside a 200: {m['provider_errors']}")
    dis = count_disagreement(payload)
    if dis:
        notes.append(dis)
    if count == 0:
        reasons.append(
            "HTTP 200 WITH AN EMPTY response ARRAY — THIS IS A FAILURE, NOT A "
            f"SUCCESS: zero fixtures visible for season {season}. This is "
            "exactly what the free tier did on 2026-07-09 while a match was "
            "live at the 45th minute")
    return _result(FAIL if reasons else PASS, m, reasons, notes)


# --------------------------------------------------------------------------
# CHECK 3 — identity stability
# --------------------------------------------------------------------------

def _collapse_initialisms(toks: list[str]) -> list[str]:
    """'D.C. United' arrives here as ['d','c','united']; join the run of
    single characters back into 'dc'. Amendment A1 — see CRITERIA
    ["check3_identity"]["initialism_rule"] and docs/APIFOOTBALL-TRIAL.md §7."""
    out: list[str] = []
    run: list[str] = []
    for t in toks:
        if len(t) == 1:
            run.append(t)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(t)
    if run:
        out.append("".join(run))
    return out


def norm_team(name: object) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s.lower())
    toks = _collapse_initialisms(s.split())
    toks = [t for t in toks if t and t not in GENERIC_TOKENS]
    if not toks:                       # never normalise a name away entirely
        toks = s.split()
    return " ".join(toks)


def token_set(name: object) -> frozenset:
    return frozenset(norm_team(name).split())


def team_id_in(fixture_payload: object, team_name: str) -> object | None:
    """The API-Football team id for this team inside one /fixtures?id= payload."""
    want = norm_team(team_name)
    for item in response_items(fixture_payload):
        for side in ("home", "away"):
            t = ((item.get("teams") or {}).get(side) or {}) \
                if isinstance(item, dict) else {}
            if t and norm_team(t.get("name")) == want:
                return t.get("id")
    return None


def check3_team_id(team_name: str, fixture_a: object, fixture_b: object,
                   fixture_a_id: object, fixture_b_id: object) -> dict:
    a, b = team_id_in(fixture_a, team_name), team_id_in(fixture_b, team_name)
    m = {"team_name": team_name, "fixture_a": fixture_a_id,
         "fixture_b": fixture_b_id, "team_id_a": a, "team_id_b": b}
    if a is None or b is None:
        return _result(INDETERMINATE, m, [
            f"could not locate {team_name!r} in both fixtures (a={a!r}, b={b!r}) "
            "— identity could not be measured, which is not the same as stable"])
    if a != b:
        return _result(FAIL, m, [
            f"UNSTABLE TEAM ID: {team_name!r} is {a} in fixture {fixture_a_id} "
            f"and {b} in fixture {fixture_b_id}"])
    return _result(PASS, m)


def _players_by_name(players_payload: object, team_id: object) -> dict:
    out = {}
    for entry in response_items(players_payload):
        if not isinstance(entry, dict):
            continue
        if team_id is not None and (entry.get("team") or {}).get("id") != team_id:
            continue
        for p in entry.get("players") or []:
            info = (p or {}).get("player") or {}
            name, pid = info.get("name"), info.get("id")
            if name and pid is not None:
                out.setdefault(str(name), pid)
    return out


def check3_player_id(players_a: object, players_b: object,
                     team_id: object) -> dict:
    a, b = _players_by_name(players_a, team_id), _players_by_name(players_b, team_id)
    shared = sorted(set(a) & set(b))
    m = {"team_id": team_id, "players_fixture_a": len(a),
         "players_fixture_b": len(b), "shared_by_name": len(shared),
         "shared_names": shared[:20]}
    if not shared:
        return _result(INDETERMINATE, m, [
            "no player appears by name in both fixtures, so player-id "
            "stability could not be measured. Pre-registered: INDETERMINATE "
            "counts as FAIL — missing evidence is not confidence"])
    mismatched = [(n, a[n], b[n]) for n in shared if a[n] != b[n]]
    m["mismatched"] = mismatched[:20]
    m["example"] = {"name": shared[0], "id_a": a[shared[0]], "id_b": b[shared[0]]}
    if mismatched:
        return _result(FAIL, m, [
            "UNSTABLE PLAYER ID: " + "; ".join(
                f"{n!r} is {x} in one fixture and {y} in the other"
                for n, x, y in mismatched[:5])])
    return _result(PASS, m)


def resolve_roster(apif_names: list, espn_names: list, aliases: dict) -> dict:
    """Resolve API-Football team names 1:1 against the frozen ESPN roster.

    Tiers, in order: exact normalised -> frozen alias -> equal token set ->
    unique containment. Anything that could match two ESPN names, and any
    ESPN name claimed by two API-Football teams, FAILS EXPLICITLY and is
    named. Nothing is ever silently picked.
    """
    by_norm: dict[str, list] = {}
    for n in espn_names:
        by_norm.setdefault(norm_team(n), []).append(n)
    alias_by_norm: dict[str, str] = {}
    alias_collisions = []
    for k, v in (aliases or {}).items():
        nk = norm_team(k)
        if nk in alias_by_norm and alias_by_norm[nk] != v:
            alias_collisions.append(
                f"alias key {k!r} normalises onto an existing alias for "
                f"{alias_by_norm[nk]!r}")
        alias_by_norm[nk] = v
    dead_aliases = [f"{k} -> {v}" for k, v in (aliases or {}).items()
                    if v not in set(espn_names)]

    rows, tiers = [], {"exact": 0, "alias": 0, "token_set": 0,
                       "containment": 0, "unresolved": 0, "ambiguous": 0}
    for raw in apif_names:
        na = norm_team(raw)
        tier, matched, cands = None, None, []

        cands = by_norm.get(na, [])
        if len(cands) == 1:
            tier, matched = "exact", cands[0]
        elif len(cands) > 1:
            tier = "ambiguous"
        else:
            alias_target = alias_by_norm.get(na)
            if alias_target is not None:
                tier, matched = "alias", alias_target
            else:
                ts = token_set(raw)
                hits = [n for n in espn_names if token_set(n) == ts]
                if len(hits) == 1:
                    tier, matched, cands = "token_set", hits[0], hits
                elif len(hits) > 1:
                    tier, cands = "ambiguous", hits
                else:
                    hits = [n for n in espn_names
                            if na and (na in norm_team(n) or norm_team(n) in na)]
                    if len(hits) == 1:
                        tier, matched, cands = "containment", hits[0], hits
                    elif len(hits) > 1:
                        tier, cands = "ambiguous", hits
                    else:
                        tier = "unresolved"
        tiers[tier] = tiers.get(tier, 0) + 1
        rows.append({"apifootball_name": raw, "tier": tier,
                     "espn_name": matched,
                     "candidates": cands if tier == "ambiguous" else []})

    # a second API-Football team claiming an already-claimed ESPN name is the
    # same defect from the other direction: a silent mis-join
    claimed: dict[str, list] = {}
    for r in rows:
        if r["espn_name"]:
            claimed.setdefault(r["espn_name"], []).append(r["apifootball_name"])
    collisions = {k: v for k, v in claimed.items() if len(v) > 1}
    for k, v in collisions.items():
        for r in rows:
            if r["espn_name"] == k:
                r["tier"] = "ambiguous"
        tiers["ambiguous"] += len(v)

    matched_rows = [r for r in rows if r["tier"] not in ("unresolved", "ambiguous")]
    total = len(rows)
    return {
        "apifootball_teams": total,
        "espn_reference_teams": len(espn_names),
        "resolved": len(matched_rows),
        "rate": (len(matched_rows) / total) if total else 0.0,
        "tiers": tiers,
        "unresolved_apifootball_names": sorted(
            r["apifootball_name"] for r in rows if r["tier"] == "unresolved"),
        "ambiguous": [
            {"apifootball_name": r["apifootball_name"],
             "candidates": r["candidates"]}
            for r in rows if r["tier"] == "ambiguous"],
        "espn_name_collisions": collisions,
        "espn_names_without_counterpart": sorted(
            set(espn_names) - {r["espn_name"] for r in matched_rows}),
        "alias_key_collisions": alias_collisions,
        "aliases_pointing_outside_the_reference": dead_aliases,
        "rows": rows,
    }


def check3_roster(league_key: str, resolution: dict) -> dict:
    thresh = CRITERIA["check3_identity"]["min_resolution_rate"][league_key]
    m = dict(resolution, threshold=thresh)
    m.pop("rows", None)
    reasons, notes = [], []
    if resolution["apifootball_teams"] == 0:
        reasons.append(
            "HTTP 200 WITH AN EMPTY response ARRAY — THIS IS A FAILURE, NOT A "
            "SUCCESS: the provider returned no teams for this league/season")
    if resolution["ambiguous"]:
        reasons.append("AMBIGUOUS IDENTITY (failing explicitly, never guessing): "
                       + "; ".join(f"{a['apifootball_name']!r} -> {a['candidates']}"
                                   for a in resolution["ambiguous"][:8]))
    if resolution["espn_name_collisions"]:
        reasons.append("two API-Football teams claim one ESPN name: "
                       + "; ".join(f"{k!r} <- {v}" for k, v in
                                   resolution["espn_name_collisions"].items()))
    if resolution["apifootball_teams"] and resolution["rate"] < thresh:
        reasons.append(
            f"resolution rate {resolution['rate']:.1%} is below the "
            f"pre-registered {thresh:.0%} for {league_key}")
    if resolution["unresolved_apifootball_names"]:
        notes.append("UNRESOLVED (API-Football side): "
                     + ", ".join(resolution["unresolved_apifootball_names"]))
    if resolution["espn_names_without_counterpart"]:
        notes.append("NO COUNTERPART (ESPN side — may be our own stale "
                     "reference, see scripts/apifootball_espn_roster.json): "
                     + ", ".join(resolution["espn_names_without_counterpart"]))
    if resolution["alias_key_collisions"]:
        notes.append("frozen alias table self-conflict: "
                     + "; ".join(resolution["alias_key_collisions"]))
    if resolution["aliases_pointing_outside_the_reference"]:
        notes.append("frozen alias targets absent from the reference roster: "
                     + "; ".join(resolution["aliases_pointing_outside_the_reference"]))
    return _result(FAIL if reasons else PASS, m, reasons, notes)


# --------------------------------------------------------------------------
# CHECK 4 — no pre-match xG (leakage)
# --------------------------------------------------------------------------

_LEAKY_KEY_HINTS = ("expected_goal", "expectedgoal", "xg")


def scan_fixture_for_xg_keys(obj: object, path: str = "") -> list[str]:
    """Any xG-shaped key anywhere on a not-started fixture object. The stats
    endpoint being empty only proves the stats endpoint is empty; a post-hoc
    field hiding on the fixture itself would leak just as hard."""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kn = norm_stat_type(k)
            here = f"{path}.{k}" if path else str(k)
            if any(h.replace("_", "") in kn for h in _LEAKY_KEY_HINTS):
                hits.append(f"{here}={v!r}")
            hits.extend(scan_fixture_for_xg_keys(v, here))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(scan_fixture_for_xg_keys(v, f"{path}[{i}]"))
    return hits


def check4_league(league_key: str, fixture_obj: object, stats_payload: object,
                  now_ts: float) -> dict:
    fx = (fixture_obj or {}).get("fixture") or {} if isinstance(fixture_obj, dict) else {}
    status = ((fx.get("status") or {}).get("short"))
    ts = fx.get("timestamp")
    m = {"league": league_key, "fixture_id": fx.get("id"), "status": status,
         "kickoff_timestamp": ts, "kickoff_utc": None,
         "seconds_until_kickoff": None}
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        m["kickoff_utc"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        m["seconds_until_kickoff"] = int(ts - now_ts)
    reasons, notes = [], []

    if status != NOT_STARTED_STATUS or not isinstance(ts, (int, float)) or \
            isinstance(ts, bool) or ts <= now_ts:
        return _result(INDETERMINATE, m, [
            "could not obtain a fixture that genuinely has not kicked off "
            f"(status={status!r}, kickoff={m['kickoff_utc']}). Pre-registered: "
            "a leakage check that cannot run counts as FAIL, never as clean"])

    f = xg_findings(stats_payload)
    m["team_entries"] = len(response_items(stats_payload))
    m["xg_statistic_type_seen"] = f["xg_statistic_type_seen_anywhere"]
    m["teams"] = f["teams"]
    m["fixture_object_xg_keys"] = scan_fixture_for_xg_keys(fixture_obj)

    populated = [t for t in f["teams"]
                 if t["xg_value"] is not None and t["xg_value"] != 0.0]
    placeholders = [t for t in f["teams"]
                    if t["xg_statistic_type"] and t["xg_value"] in (None, 0.0)]

    if m["fixture_object_xg_keys"]:
        notes.append("xG-shaped keys on the not-started fixture object itself: "
                     + "; ".join(m["fixture_object_xg_keys"][:8]))
    if placeholders:
        notes.append("pre-match xG placeholders present but empty/zero, which "
                     "carry no outcome information: " + ", ".join(
                         f"{t['team_name']}={t['xg_raw']!r}" for t in placeholders))
    if not response_items(stats_payload):
        notes.append("statistics response is empty for a not-started fixture — "
                     "here that IS the desired result, and it is the one place "
                     "in this harness where an empty payload is not a failure")

    if populated:
        reasons.append(
            "CRITICAL FAIL — PRE-MATCH xG IS POPULATED: " + ", ".join(
                f"{t['team_name']}={t['xg_value']}" for t in populated)
            + f" on fixture {m['fixture_id']}, which has not kicked off "
              f"(kickoff {m['kickoff_utc']}). An xG feature built on this would "
              "silently carry the outcome. This repo treats that as a "
              "DISQUALIFYING DEFECT: target leakage through a provider's "
              "post-hoc field.")
        return _result(CRITICAL_FAIL, m, reasons, notes)
    return _result(PASS, m, reasons, notes)


# --------------------------------------------------------------------------
# season derivation
# --------------------------------------------------------------------------

def calendar_season(style: str, now: datetime) -> int:
    """Which season label an active-today slate lives under. Deliberately
    derived from the calendar rather than read off the provider's `current`
    flag, whose season list is plan-filtered."""
    if style == "calendar_year":
        return now.year
    return now.year if now.month >= 7 else now.year - 1


def pick_completed_fixture(fixtures: list, now_ts: float) -> tuple[dict | None, str]:
    """The most recent completed fixture at least 24h old, so that statistics
    have had time to be posted. Falls back to the most recent completed
    fixture and says so. Deterministic: nothing here can cherry-pick."""
    done = []
    for f in fixtures:
        if not isinstance(f, dict):
            continue
        fx = f.get("fixture") or {}
        if ((fx.get("status") or {}).get("short")) in FINISHED_STATUSES:
            ts = fx.get("timestamp")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                done.append((ts, f))
    if not done:
        return None, "no completed fixture in this season"
    done.sort(key=lambda p: p[0], reverse=True)
    settled = [p for p in done if (now_ts - p[0]) >= 86400]
    if settled:
        return settled[0][1], "most recent completed fixture at least 24h old"
    return done[0][1], ("most recent completed fixture, LESS THAN 24h OLD — "
                        "statistics may not be posted yet")


def pick_fixture_pair_for_one_team(fixtures: list, now_ts: float) -> tuple | None:
    """One team and two DIFFERENT completed fixtures it played in.

    Deterministic and stated in advance so nothing can be cherry-picked: take
    the most recent completed fixture, take its HOME team, then take that
    team's next-most-recent completed fixture. Returns
    (team_name, team_id, fixture_a_id, fixture_b_id) or None.
    """
    anchor, _ = pick_completed_fixture(fixtures, now_ts)
    if anchor is None:
        return None
    home = ((anchor.get("teams") or {}).get("home") or {})
    name, tid = home.get("name"), home.get("id")
    fa_id = (anchor.get("fixture") or {}).get("id")
    if not name or fa_id is None:
        return None

    others = []
    for f in fixtures:
        if not isinstance(f, dict):
            continue
        fx = f.get("fixture") or {}
        if ((fx.get("status") or {}).get("short")) not in FINISHED_STATUSES:
            continue
        if fx.get("id") == fa_id:
            continue
        sides = (f.get("teams") or {})
        ids = {((sides.get(s) or {}).get("id")) for s in ("home", "away")}
        names = {norm_team((sides.get(s) or {}).get("name"))
                 for s in ("home", "away")}
        if (tid is not None and tid in ids) or norm_team(name) in names:
            ts = fx.get("timestamp")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                others.append((ts, fx.get("id")))
    if not others:
        return None
    others.sort(key=lambda p: p[0], reverse=True)
    return (name, tid, fa_id, others[0][1])


def pick_not_started_fixture(fixtures: list, now_ts: float) -> dict | None:
    """The SOONEST genuinely-unplayed fixture: status NS and kickoff in the
    future. Soonest, not any, so the check runs against the fixture most
    likely to have provider data attached."""
    upcoming = []
    for f in fixtures:
        if not isinstance(f, dict):
            continue
        fx = f.get("fixture") or {}
        if ((fx.get("status") or {}).get("short")) != NOT_STARTED_STATUS:
            continue
        ts = fx.get("timestamp")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > now_ts:
            upcoming.append((ts, f))
    if not upcoming:
        return None
    upcoming.sort(key=lambda p: p[0])
    return upcoming[0][1]


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

class BudgetExceeded(RuntimeError):
    pass


def scrub_pii(body: object) -> object:
    """Drop the account holder's identity from anything on its way to the
    archive.

    /status returns response.account = {firstname, lastname, email}. Redacting
    it in the summary is not enough — the raw recorded body is what actually
    gets committed, and the first live run of this harness put Son's name and
    email into a file destined for the repo. Applied to EVERY recorded body,
    not just /status, so a future endpoint that starts returning an account
    block is covered without anyone having to notice.
    """
    if not isinstance(body, dict):
        return body
    resp = body.get("response")
    if isinstance(resp, dict) and "account" in resp:
        cleaned = dict(resp)
        cleaned["account"] = "<ACCOUNT_HOLDER_PII_DROPPED_BY_HARNESS>"
        return dict(body, response=cleaned)
    return body


class Client:
    """One budgeted, delayed, redacting GET path. No tight retries."""

    def __init__(self, key: str, base: str, out: list, say):
        self._key = key
        self._base = base.rstrip("/")
        self.records = out
        self._say = say
        self.used = 0
        b = CRITERIA["request_budget"]
        self._cap = b["hard_cap"]
        self._delay = b["delay_seconds"]
        self._retries = b["max_retries_per_request"]
        self._retry_delay = b["retry_delay_seconds"]
        self._timeout = b["timeout_seconds"]
        self._session = requests.Session()

    def get(self, path: str, params: dict, label: str) -> object:
        if self.used >= self._cap:
            raise BudgetExceeded(
                f"request cap {self._cap} reached before {label} — aborting "
                "rather than spending more of the day's quota")
        attempt, last = 0, None
        while attempt <= self._retries:
            if self.used:
                time.sleep(self._delay)
            self.used += 1
            t0 = time.time()
            try:
                r = self._session.get(
                    f"{self._base}/{path.lstrip('/')}", params=params,
                    headers={"x-apisports-key": self._key},
                    timeout=self._timeout)
            except Exception as exc:                       # transport failure
                last = f"{type(exc).__name__}: {_redact(exc, self._key)}"
                self.records.append({"label": label, "path": path,
                                     "params": params, "transport_error": last,
                                     "attempt": attempt + 1})
                attempt += 1
                if attempt <= self._retries:
                    self._say(f"    transport error on {label}; one retry in "
                              f"{self._retry_delay}s")
                    time.sleep(self._retry_delay)
                continue
            elapsed = round((time.time() - t0) * 1000)
            try:
                body = r.json()
            except Exception:
                body = {"_unparseable_body": _redact(r.text[:2000], self._key)}
            rec = {"label": label, "path": path, "params": params,
                   "http_status": r.status_code, "elapsed_ms": elapsed,
                   "attempt": attempt + 1,
                   "response_array_length": len(response_items(body)),
                   "provider_results_field": declared_results(body),
                   "provider_errors": errors_of(body),
                   "rate_limit_remaining_minute": r.headers.get(
                       "x-ratelimit-remaining"),
                   "rate_limit_remaining_day": r.headers.get(
                       "x-ratelimit-requests-remaining"),
                   "body": scrub_pii(body)}
            self.records.append(rec)
            # never hammer a spent quota
            rem = rec["rate_limit_remaining_day"]
            if rem is not None and str(rem).strip() in {"0", "0.0"}:
                raise BudgetExceeded(
                    "provider reports 0 daily requests remaining — aborting")
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                last = f"HTTP {r.status_code}"
                attempt += 1
                if attempt <= self._retries:
                    self._say(f"    {last} on {label}; one retry in "
                              f"{self._retry_delay}s")
                    time.sleep(self._retry_delay)
                continue
            return body
        raise RuntimeError(f"{label} failed after {attempt} attempt(s): {last}")


def sanitise_status(body: object) -> dict:
    """/status carries the account holder's name and email. Keep the plan and
    the quota; drop the PII before it reaches an archive that gets reviewed."""
    resp = body.get("response") if isinstance(body, dict) else None
    if not isinstance(resp, dict):
        return {"unavailable": True}
    return {"subscription": resp.get("subscription"),
            "requests": resp.get("requests"),
            "_account_block_dropped": "account" in resp}


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def _bar(label: str, status: str, width: int = 58) -> str:
    dots = "." * max(3, width - len(label))
    return f"{label} {dots} {status}"


def run(key: str, base: str, roster: dict, now: datetime, say) -> dict:
    now_ts = now.timestamp()
    records: list = []
    client = Client(key, base, records, say)
    leagues = roster["leagues"]
    report: dict = {"per_league": {}, "checks": {}}

    say("Reading plan status ...")
    status_body = client.get("status", {}, "status")
    report["plan_status"] = sanitise_status(status_body)
    sub = (report["plan_status"].get("subscription") or {})
    say(f"  plan={sub.get('plan')!r} active={sub.get('active')!r} "
        f"quota={report['plan_status'].get('requests')}")

    # ---- per-league discovery -------------------------------------------
    for lk, cfg in leagues.items():
        lid = cfg["apifootball_league_id"]
        info: dict = {"league_id": lid, "espn_slug": cfg["espn_league_slug"]}
        say(f"\n[{lk}] league id {lid} ({cfg['display']})")

        lb = client.get("leagues", {"id": lid}, f"leagues:{lk}")
        items = response_items(lb)
        if not items:
            info["identity_error"] = (
                "HTTP 200 WITH AN EMPTY response ARRAY — THIS IS A FAILURE: "
                f"league id {lid} returned no league record")
            info["seasons_visible"] = []
            report["per_league"][lk] = info
            say(f"  !! {info['identity_error']}")
            continue
        lg = (items[0].get("league") or {})
        ctry = (items[0].get("country") or {})
        info["provider_league_name"] = lg.get("name")
        info["provider_country"] = ctry.get("name")
        want = cfg["apifootball_league_name_contains"].lower()
        if want not in str(lg.get("name") or "").lower():
            info["identity_error"] = (
                f"league id {lid} is {lg.get('name')!r} ({ctry.get('name')!r}), "
                f"which does not contain {want!r} — measuring the wrong "
                "competition would invalidate every number below")
            say(f"  !! {info['identity_error']}")
        seasons = [s for s in (items[0].get("seasons") or [])
                   if isinstance(s, dict)]
        info["seasons_visible"] = sorted(
            [s.get("year") for s in seasons if isinstance(s.get("year"), int)])
        info["provider_current_season"] = next(
            (s.get("year") for s in seasons if s.get("current") is True), None)
        info["provider_declared_statistics_coverage"] = next(
            (((s.get("coverage") or {}).get("fixtures") or {})
             .get("statistics_fixtures")
             for s in seasons if s.get("current") is True), None)
        info["calendar_season"] = calendar_season(cfg["season_label_style"], now)
        say(f"  seasons visible: {info['seasons_visible']}")
        say(f"  provider 'current' season: {info['provider_current_season']}  |  "
            f"calendar-derived season: {info['calendar_season']}")
        say("  provider SELF-DECLARED statistics coverage: "
            f"{info['provider_declared_statistics_coverage']} "
            "(context only — never a substitute for the measurement)")

        season = info["calendar_season"]
        fb = client.get("fixtures", {"league": lid, "season": season},
                        f"fixtures:{lk}:{season}")
        info["current_season_payload"] = {"season": season, "body_ref": len(records) - 1}
        info["check2"] = check2_league(lk, season, fb)
        fixtures = response_items(fb)
        say(f"  season {season}: {len(fixtures)} fixtures counted "
            f"(provider results field {declared_results(fb)!r})")

        # a zero here is the free tier's signature; diagnose it once, cheaply
        if not fixtures and info["provider_current_season"] not in (None, season):
            alt = info["provider_current_season"]
            say(f"  season {season} was EMPTY; probing the provider's own "
                f"'current' season {alt} once, as a diagnosis")
            ab = client.get("fixtures", {"league": lid, "season": alt},
                            f"fixtures:{lk}:{alt}")
            info["diagnostic_provider_current_season_count"] = len(response_items(ab))
            info["check2"]["notes"].append(
                f"season {season} returned 0 fixtures while the provider's own "
                f"'current' season {alt} returned "
                f"{info['diagnostic_provider_current_season_count']} — the plan "
                "cannot see the season an active slate lives in")
            if not fixtures:
                fixtures = response_items(ab)
                info["fixtures_source_season"] = alt

        # completed fixture for CHECK 1
        chosen, how = pick_completed_fixture(fixtures, now_ts)
        used_season = info.get("fixtures_source_season", season)
        if chosen is None and info["seasons_visible"]:
            earlier = [y for y in info["seasons_visible"] if y < season]
            if earlier:
                alt = max(earlier)
                say(f"  no completed fixture in season {used_season}; falling "
                    f"back ONCE to season {alt}")
                pb = client.get("fixtures", {"league": lid, "season": alt},
                                f"fixtures:{lk}:{alt}")
                chosen, how = pick_completed_fixture(response_items(pb), now_ts)
                if chosen is not None:
                    used_season = alt
        info["check1_season_used"] = used_season
        info["check1_fixture_selection"] = how
        info["completed_fixture"] = chosen

        # not-started fixture for CHECK 4 (from the current-season pull)
        info["not_started_fixture"] = pick_not_started_fixture(fixtures, now_ts)
        info["fixture_pair"] = pick_fixture_pair_for_one_team(fixtures, now_ts)
        report["per_league"][lk] = info

    # ---- CHECK 1 ---------------------------------------------------------
    say("\nCHECK 1 — expected goals on a completed fixture, per league")
    c1 = {}
    for lk, info in report["per_league"].items():
        chosen = info.get("completed_fixture")
        if chosen is None:
            c1[lk] = _result(INDETERMINATE,
                             {"league": lk, "season": info.get("check1_season_used")},
                             ["no completed fixture was visible in any season "
                              "this plan lists, so xG presence could not be "
                              "measured — INDETERMINATE, which counts as FAIL"])
            say(f"  {lk:8s} {c1[lk]['status']} — {c1[lk]['reasons'][0]}")
            continue
        fid = (chosen.get("fixture") or {}).get("id")
        sb = client.get("fixtures/statistics", {"fixture": fid},
                        f"statistics:{lk}:{fid}")
        c1[lk] = check1_league(lk, info.get("check1_season_used"), fid, sb)
        vals = ", ".join(
            f"{t['team_name']}={t['xg_value'] if t['xg_value'] is not None else 'ABSENT'}"
            for t in c1[lk]["measurements"].get("teams", []))
        say(f"  {lk:8s} season {info.get('check1_season_used')} fixture {fid} "
            f"-> {c1[lk]['status']}  {vals}")
        for r in c1[lk]["reasons"]:
            say(f"           {r}")
    report["checks"]["check1"] = c1

    # ---- CHECK 2 ---------------------------------------------------------
    report["checks"]["check2"] = {
        lk: info.get("check2") or _result(
            INDETERMINATE, {"league": lk},
            ["league record was unreadable, so current-season visibility "
             "could not be measured"])
        for lk, info in report["per_league"].items()}

    # ---- CHECK 3 ---------------------------------------------------------
    say("\nCHECK 3 — identity stability")
    c3: dict = {"team_id": None, "player_id": None, "rosters": {}}
    anchor = "mls" if "mls" in report["per_league"] else \
        next(iter(report["per_league"]), None)
    ainfo = report["per_league"].get(anchor) or {}
    pair = ainfo.get("fixture_pair")
    if pair is None:
        # fall back to any league that did yield a pair, and say which
        for lk, info in report["per_league"].items():
            if info.get("fixture_pair"):
                anchor, pair = lk, info["fixture_pair"]
                say(f"  anchor league fell back to {lk} (MLS yielded no pair)")
                break
    if pair is None:
        c3["team_id"] = _result(
            INDETERMINATE, {"anchor_league": anchor},
            ["could not find one team with two completed fixtures in the "
             "anchor league, so team-id stability could not be measured"])
        c3["player_id"] = _result(
            INDETERMINATE, {"anchor_league": anchor},
            ["skipped: no fixture pair"])
    else:
        team_name, team_id_hint, fa_id, fb_id = pair
        say(f"  anchor {anchor}: {team_name!r} across fixtures {fa_id} and {fb_id}")
        fa = client.get("fixtures", {"id": fa_id}, f"fixture:{fa_id}")
        fb2 = client.get("fixtures", {"id": fb_id}, f"fixture:{fb_id}")
        c3["team_id"] = check3_team_id(team_name, fa, fb2, fa_id, fb_id)
        say(f"  team id  {team_name!r}: "
            f"{c3['team_id']['measurements']['team_id_a']} vs "
            f"{c3['team_id']['measurements']['team_id_b']} -> "
            f"{c3['team_id']['status']}")
        pa = client.get("fixtures/players", {"fixture": fa_id}, f"players:{fa_id}")
        pb = client.get("fixtures/players", {"fixture": fb_id}, f"players:{fb_id}")
        c3["player_id"] = check3_player_id(pa, pb, team_id_hint)
        pm = c3["player_id"]["measurements"]
        say(f"  player id shared-by-name {pm.get('shared_by_name')} "
            f"example {pm.get('example')} -> {c3['player_id']['status']}")
    for r in (c3["team_id"]["reasons"] + c3["player_id"]["reasons"]):
        say(f"           {r}")

    say("  roster resolution against the FROZEN ESPN reference")
    for lk, cfg in leagues.items():
        info = report["per_league"].get(lk) or {}
        season = info.get("fixtures_source_season", info.get("calendar_season"))
        tb = client.get("teams", {"league": cfg["apifootball_league_id"],
                                  "season": season}, f"teams:{lk}:{season}")
        names = [((i.get("team") or {}).get("name")) for i in response_items(tb)]
        names = [n for n in names if n]
        res = resolve_roster(names, cfg["espn_team_names"], cfg["aliases"])
        c3["rosters"][lk] = check3_roster(lk, res)
        r = c3["rosters"][lk]
        say(f"    {lk:8s} season {season}  {res['resolved']}/"
            f"{res['apifootball_teams']} = {res['rate']:.1%} "
            f"(threshold {r['measurements']['threshold']:.0%})  "
            f"tiers {res['tiers']}  -> {r['status']}")
        for line in r["reasons"] + r["notes"]:
            say(f"             {line}")
    report["checks"]["check3"] = c3

    # ---- CHECK 4 ---------------------------------------------------------
    say("\nCHECK 4 — no pre-match xG (leakage)")
    c4 = {}
    for lk, info in report["per_league"].items():
        ns = info.get("not_started_fixture")
        if ns is None:
            c4[lk] = _result(INDETERMINATE, {"league": lk},
                             ["no not-yet-kicked-off fixture was visible, so "
                              "pre-match leakage could not be tested. "
                              "Pre-registered: this counts as FAIL — a leakage "
                              "check that cannot run must never read as clean"])
            say(f"  {lk:8s} {c4[lk]['status']} — {c4[lk]['reasons'][0]}")
            continue
        fid = (ns.get("fixture") or {}).get("id")
        sb = client.get("fixtures/statistics", {"fixture": fid},
                        f"prematch_statistics:{lk}:{fid}")
        c4[lk] = check4_league(lk, ns, sb, now_ts)
        m = c4[lk]["measurements"]
        say(f"  {lk:8s} fixture {fid} NS kickoff {m.get('kickoff_utc')} "
            f"-> {c4[lk]['status']}")
        for line in c4[lk]["reasons"] + c4[lk]["notes"]:
            say(f"           {line}")
    report["checks"]["check4"] = c4

    report["requests_used"] = client.used
    report["request_records"] = records
    return report


def verdict(report: dict) -> tuple[str, int, list[str]]:
    """KEEP only if all four pass. Otherwise DROP, naming the failing check."""
    c = report.get("checks", {})
    failing: list[str] = []
    critical = False

    def worst(group: dict) -> tuple[str, list[str]]:
        bad = [k for k, v in group.items() if v["status"] in FAILING]
        st = PASS if not bad else (
            CRITICAL_FAIL if any(group[k]["status"] == CRITICAL_FAIL for k in bad)
            else FAIL)
        return st, bad

    s1, bad1 = worst(c.get("check1", {}))
    if s1 != PASS:
        failing.append(f"CHECK 1 (xG presence) failed for: {', '.join(bad1)}")
    s2, bad2 = worst(c.get("check2", {}))
    if s2 != PASS:
        failing.append(f"CHECK 2 (current-season visibility) failed for: "
                       f"{', '.join(bad2)}")
    c3 = c.get("check3", {})
    parts = {"team_id": c3.get("team_id") or {"status": INDETERMINATE},
             "player_id": c3.get("player_id") or {"status": INDETERMINATE}}
    parts.update({f"roster:{k}": v for k, v in (c3.get("rosters") or {}).items()})
    s3, bad3 = worst(parts)
    if s3 != PASS:
        failing.append(f"CHECK 3 (identity stability) failed for: {', '.join(bad3)}")
    s4, bad4 = worst(c.get("check4", {}))
    if s4 != PASS:
        critical = any((c.get("check4", {}).get(k) or {}).get("status")
                       == CRITICAL_FAIL for k in bad4)
        failing.append(f"CHECK 4 (pre-match xG leakage) failed for: "
                       f"{', '.join(bad4)}")

    if not failing:
        return "KEEP", 0, []
    return "DROP", (3 if critical else 1), failing


def print_verdict(report: dict, say) -> tuple[str, int]:
    c = report.get("checks", {})
    v, code, failing = verdict(report)
    say("\n" + "=" * 78)
    say("VERDICT BLOCK")
    say("=" * 78)

    def group_status(group: dict) -> str:
        bad = [k for k, x in group.items() if x["status"] in FAILING]
        if not bad:
            return PASS
        if any(group[k]["status"] == CRITICAL_FAIL for k in bad):
            return CRITICAL_FAIL
        return FAIL

    say(_bar("CHECK 1  xG present for all four leagues",
             group_status(c.get("check1", {}))))
    for lk, r in (c.get("check1") or {}).items():
        teams = r["measurements"].get("teams") or []
        vals = " / ".join(
            f"{t['team_name']}={t['xg_value'] if t['xg_value'] is not None else 'ABSENT'}"
            for t in teams) or "no team entries"
        say(f"    {lk:8s} season {r['measurements'].get('season')} "
            f"fixture {r['measurements'].get('fixture_id')}  {r['status']}  {vals}")
        if r["measurements"].get("xg_statistic_type_seen") is False:
            offered = ", ".join(
                (r["measurements"].get("statistic_types_offered") or [])[:12])
            say("             xG STATISTIC TYPE ABSENT FROM THE RESPONSE "
                "ENTIRELY. Types offered: " + (offered or "(none)"))

    say(_bar("CHECK 2  current-season visibility",
             group_status(c.get("check2", {}))))
    for lk, r in (c.get("check2") or {}).items():
        m = r["measurements"]
        say(f"    {lk:8s} season {m.get('season_requested')}  "
            f"fixtures counted {m.get('fixtures_counted')}  "
            f"(provider results field {m.get('provider_results_field')!r})  "
            f"{r['status']}")
        for line in r["reasons"] + r["notes"]:
            say(f"             {line}")

    c3 = c.get("check3", {})
    parts = {"team_id": c3.get("team_id") or {"status": INDETERMINATE},
             "player_id": c3.get("player_id") or {"status": INDETERMINATE}}
    parts.update({f"roster:{k}": v for k, v in (c3.get("rosters") or {}).items()})
    say(_bar("CHECK 3  identity stability", group_status(parts)))
    tm = (c3.get("team_id") or {}).get("measurements", {})
    say(f"    team id    {tm.get('team_name')!r}  fixture {tm.get('fixture_a')} "
        f"-> {tm.get('team_id_a')}  |  fixture {tm.get('fixture_b')} -> "
        f"{tm.get('team_id_b')}   {(c3.get('team_id') or {}).get('status')}")
    pm = (c3.get("player_id") or {}).get("measurements", {})
    say(f"    player id  shared by name {pm.get('shared_by_name')}  "
        f"example {pm.get('example')}   "
        f"{(c3.get('player_id') or {}).get('status')}")
    for lk, r in (c3.get("rosters") or {}).items():
        m = r["measurements"]
        say(f"    roster {lk:8s} {m.get('resolved')}/{m.get('apifootball_teams')}"
            f" = {(m.get('rate') or 0):.1%}  threshold "
            f"{(m.get('threshold') or 0):.0%}  tiers {m.get('tiers')}  "
            f"{r['status']}")
        for line in r["reasons"] + r["notes"]:
            say(f"             {line}")

    say(_bar("CHECK 4  no pre-match xG (leakage)",
             group_status(c.get("check4", {}))))
    for lk, r in (c.get("check4") or {}).items():
        m = r["measurements"]
        say(f"    {lk:8s} fixture {m.get('fixture_id')} status "
            f"{m.get('status')!r} kickoff {m.get('kickoff_utc')}  {r['status']}")
        for line in r["reasons"] + r["notes"]:
            say(f"             {line}")

    say("-" * 78)
    if v == "KEEP":
        say("VERDICT: KEEP — all four pre-registered checks passed.")
    else:
        for f in failing:
            say(f"VERDICT: DROP — {f}")
        if code == 3:
            say("VERDICT: DROP is CRITICAL — pre-match xG was populated. An xG "
                "feature could silently carry the outcome; this repo treats "
                "that as a disqualifying defect.")
    say(f"requests used: {report.get('requests_used')} of "
        f"{CRITERIA['request_budget']['hard_cap']} cap  "
        f"(plan limits {CRITERIA['request_budget']['plan_limits']})")
    say("=" * 78)
    return v, code


def archive_path(now: datetime, exists=None) -> Path:
    """The dated archive, never clobbering an earlier run's evidence.

    A second run on the same UTC date would otherwise overwrite the first —
    which is how a pre-registered result quietly disappears the moment
    somebody re-runs after a fix. Later runs get .run2, .run3 and so on.
    """
    exists = Path.exists if exists is None else exists
    base = ARCHIVE_DIR / f"apifootball_verification_{now.strftime('%Y-%m-%d')}.json"
    if not exists(base):
        return base
    for n in range(2, 100):
        cand = base.with_suffix(f".run{n}.json")
        if not exists(cand):
            return cand
    raise RuntimeError(f"refusing to overwrite: 99 archives already exist for "
                       f"{now.strftime('%Y-%m-%d')}")


def write_archive(path: Path, doc: dict, key: str) -> None:
    """Serialise, then scrub the whole string. Scrubbing the rendered bytes
    rather than walking the tree is deliberate: it cannot miss a nested or
    unexpected occurrence of the key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(doc, indent=1, ensure_ascii=False, default=str)
    blob = _redact(blob, key)
    if key and key in blob:                     # unreachable; assert anyway
        raise RuntimeError("refusing to write an archive containing the key")
    path.write_text(blob + "\n", encoding="utf-8")


def main(argv=None) -> int:
    lines: list[str] = []
    key, key_source, key_problems = load_key()

    def say(msg: str = "") -> None:
        text = _redact(msg, key)
        lines.append(text)
        print(text, flush=True)

    fp = criteria_fingerprint()
    if fp != CRITERIA_FINGERPRINT:
        print("REFUSING TO RUN: the pre-registered criteria have been edited "
              "without updating CRITERIA_FINGERPRINT.")
        print(f"  expected {CRITERIA_FINGERPRINT}")
        print(f"  computed {fp}")
        print("This guard exists so a threshold cannot be retuned to fit the "
              "data without the change appearing in the diff.")
        return 2

    rfp = roster_fingerprint()
    if rfp != ROSTER_FINGERPRINT:
        print("REFUSING TO RUN: the frozen ESPN reference roster has been "
              "edited without updating ROSTER_FINGERPRINT.")
        print(f"  expected {ROSTER_FINGERPRINT}")
        print(f"  computed {rfp}")
        print(f"  file     {ROSTER_PATH}")
        print("The roster is half the instrument. Editing it to contain a name "
              "that just failed CHECK 3 turns a DROP into a KEEP without "
              "touching a single threshold — which is what happened on "
              "2026-07-29 (research_archive/"
              "apifootball_tuning_incident_2026-07-29.json). If the change is "
              "intended, record it as a post-registration amendment in "
              "docs/APIFOOTBALL-TRIAL.md §7 and update the literal.")
        return 2

    if not key:
        print("NO KEY AVAILABLE — nothing was measured.\n")
        for p in key_problems:
            print(f"  PROBLEM: {p}")
        if key_problems:
            print()
        print("The harness looks in two places, in this order:\n")
        print("  1. the APIFOOTBALL_KEY environment variable")
        print(f"  2. {KEY_FILE}, mode 600, key on one line\n")
        print("Preferred, because the key never touches a shell history or an "
              "agent transcript:\n")
        print(f"    install -m 600 /dev/null {KEY_FILE}")
        print(f"    # then paste the key into {KEY_FILE} with an editor")
        print(f"    python scripts/verify_apifootball.py\n")
        print("Or, for one command only:\n")
        print("    APIFOOTBALL_KEY='<the key>' python "
              "scripts/verify_apifootball.py\n")
        print("Do not put it in .env, in a commit, or in this repo's older "
              "API_FOOTBALL_KEY variable.")
        if os.getenv("API_FOOTBALL_KEY", "").strip():
            print("\nNOTE: the repo's older API_FOOTBALL_KEY *is* set, and this "
                  "harness deliberately ignores it. It may hold the free-tier "
                  "key, and the free tier was measured season-blind — "
                  "verifying it would produce a confident answer about the "
                  "wrong plan.")
        return 2

    base = os.getenv("APIFOOTBALL_BASE", "https://v3.football.api-sports.io")
    now = datetime.now(timezone.utc)
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))

    say("=" * 78)
    say(f"API-FOOTBALL PRE-REGISTERED VERIFICATION — {now.isoformat()}")
    say(f"criteria v{CRITERIA['version']}  fingerprint {fp[:16]}  "
        f"registered {CRITERIA['registered_utc']}, before any key existed")
    say(f"ESPN reference frozen {roster['_registered_utc']} "
        f"({ROSTER_PATH.name}) sha {rfp[:16]} — matches committed literal")
    say(f"key source: {key_source} (value never printed or archived)")
    say("GOVERNING RULE: " + CRITERIA["governing_rule"])
    say("=" * 78)

    report: dict = {}
    fatal = None
    try:
        report = run(key, base, roster, now, say)
    except (BudgetExceeded, RuntimeError) as exc:
        fatal = _redact(f"{type(exc).__name__}: {exc}", key)
        say(f"\nHARNESS ABORTED: {fatal}")

    if fatal and not report.get("checks"):
        code, v = 2, "INDETERMINATE"
        say("\nVERDICT: INDETERMINATE — the harness could not measure. This is "
            "neither KEEP nor DROP; missing evidence is not converted into a "
            "verdict.")
    else:
        v, code = print_verdict(report, say)
        if fatal:
            say("NOTE: the run aborted partway through, so any check above that "
                "reads INDETERMINATE was not measured rather than measured bad.")
            v, code = "INDETERMINATE", 2
            say("VERDICT downgraded to INDETERMINATE because the run was "
                "incomplete.")

    doc = {
        "_what": "Evidence for the pre-registered API-Football trial decision.",
        "_governing_rule": CRITERIA["governing_rule"],
        "_how_to_read_this": (
            "Every check below was fixed in scripts/verify_apifootball.py "
            "before an API key existed. criteria_fingerprint is the sha256 of "
            "the criteria block: if it does not match the value committed in "
            "that script, the criteria were edited and this run is not a "
            "pre-registered result."
        ),
        "run_utc": now.isoformat(),
        "verdict": v,
        "exit_code": code,
        "criteria_fingerprint": fp,
        "criteria": CRITERIA,
        "espn_reference": {
            "path": str(ROSTER_PATH.relative_to(REPO_ROOT)),
            "registered_utc": roster["_registered_utc"],
            "sha256": rfp,
            "asserted_against_committed_literal": ROSTER_FINGERPRINT,
        },
        "harness_sha256": hashlib.sha256(
            Path(__file__).resolve().read_bytes()).hexdigest(),
        "aborted": fatal,
        "report": report,
        "stdout": lines,
    }
    out = archive_path(now)
    write_archive(out, doc, key)
    print(f"\nArchive: {out.relative_to(REPO_ROOT)}  (key redacted)")
    print(f"exit {code}")
    return code


if __name__ == "__main__":                                  # pragma: no cover
    sys.exit(main())
