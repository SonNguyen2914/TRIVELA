#!/usr/bin/env python3
"""Empirical coverage probe: does API-Football serve LIVE in-play statistics,
and for which competitions?

WHY THIS EXISTS
    `docs/APIFOOTBALL-TRIAL.md` measured that *post-match* xG exists for four
    leagues. It says nothing about in-play statistics, and the hunter's
    IN_PLAY_OVERREACTION detector needs exactly that: does
    `fixtures/statistics` update DURING a match, and does `fixtures/events`
    carry a timed feed with red cards?

    Coverage is UNEVEN and undocumented. It therefore has to be MEASURED per
    competition, recorded with the date measured, and never assumed. This
    script is the measurement; `src/live/apifootball_live.py` is the runtime
    consumer of the same rules.

THE MEASUREMENT RULE — FIXED BEFORE ANY LIVE PAYLOAD WAS SEEN
    Written 2026-07-29 before this script was run once. It inherits the
    trial's governing rule verbatim:

        ASSERT A NON-ZERO COUNT, NEVER A STATUS CODE.
        A 200 WITH NOTHING IN IT IS ABSENCE, AND IS REPORTED AS ABSENCE.

    Per probed fixture, three INDEPENDENT verdicts, because they fail
    independently in the wild:

      stats_present   len(response) > 0 AND at least one entry carries at
                      least one statistic whose `type` is non-empty.
      xg_present      a non-against expected-goals type exists AND at least
                      one of its values PARSES AS A NUMBER. A `null`, '',
                      '-' or non-numeric value is ABSENT — never zero. A
                      parseable 0.0 is recorded as `xg_zero_valued`, counted
                      as PRESENT-but-zero and reported separately, because at
                      minute 3 a genuine 0.0 is real information while a
                      placeholder 0.0 is not, and this probe cannot tell them
                      apart. It says so rather than choosing.
      events_present  len(response) > 0 on `fixtures/events`.

    A competition's verdict is the UNION over its probed fixtures: if any
    probed fixture carried stats, the competition can serve them. The
    denominator (how many fixtures were probed, at what minute) travels with
    the verdict, because "1 of 1 at minute 4" and "3 of 3 at minute 70" are
    not the same evidence and must not read the same.

    A fixture probed EARLY is not evidence of absence: the trial's own table
    shows a friendly at 5' with zero stat types. Every verdict therefore
    carries `elapsed` and absence below MIN_HONEST_ELAPSED is recorded as
    `too_early_to_conclude`, NOT as absence.

BUDGET
    PRO is 300 requests/minute and 7,500/day. This probe is hard-capped at
    HARD_CAP requests with a fixed delay between them and no tight retries.
    The count used is printed and archived, alongside the provider's own
    remaining-quota headers.

SECRETS
    The key is read from APIFOOTBALL_KEY, else ~/.apifootball_key which must
    be mode 600. It travels only in a request header, is never printed, never
    placed in a URL or a param, and every byte bound for stdout or the archive
    passes through _redact() first. Account PII is dropped at the record
    layer.

    The repo's older API_FOOTBALL_KEY is deliberately NOT a fallback: it may
    hold the free-tier key, and the free tier was measured season-blind.

USAGE
    python scripts/probe_hunter_live_stats.py            # no arguments
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = REPO_ROOT / "research_archive"
REDACTED = "<APIFOOTBALL_KEY_REDACTED>"
BASE = "https://v3.football.api-sports.io"

# --- the pre-fixed measurement rule ---------------------------------------
RULE = {
    "version": "1.0",
    "registered_utc": "2026-07-29",
    "registered_before_any_live_payload_was_seen": True,
    "governing_rule": (
        "Assert a non-zero COUNT, never a status code. An HTTP 200 carrying "
        "an empty payload is ABSENCE and is reported as absence."),
    "stats_present_means": (
        "len(response) > 0 AND at least one entry carries at least one "
        "statistic with a non-empty `type`"),
    "xg_present_means": (
        "a non-against expected-goals type exists AND at least one of its "
        "values parses as a number; null/''/'-'/non-numeric is ABSENT, "
        "never zero"),
    "xg_zero_handling": (
        "a parseable 0.0 is PRESENT-but-zero, recorded separately as "
        "xg_zero_valued; this probe cannot distinguish a genuine early-match "
        "0.0 from a placeholder and does not pretend to"),
    "events_present_means": "len(response) > 0 on fixtures/events",
    "competition_verdict": (
        "UNION over probed fixtures, always reported WITH the denominator "
        "(fixtures probed, and the elapsed minute of each)"),
    "absence_below_min_elapsed": (
        "absence at elapsed < MIN_HONEST_ELAPSED is recorded as "
        "too_early_to_conclude, NEVER as absence"),
    "xg_type_reject_substrings": ["against", "conceded", "prevented",
                                  "allowed", "opponent", "xga"],
    "xg_type_accept_exact": ["xg"],
    "xg_type_accept_substring": "expectedgoals",
}

MIN_HONEST_ELAPSED = 20          # minutes; below this, absence proves nothing
HARD_CAP = 60                    # requests; abort rather than spend more
DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20
MAX_FIXTURES_PROBED = 24
MAX_PER_LEAGUE = 2               # spread the sample across competitions


class BudgetExceeded(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- secrets ---------------------------------------------------------------
KEY_FILE = Path.home() / ".apifootball_key"


def load_key() -> tuple[str, str, list[str]]:
    """(key, where_from, problems). Mode-600 enforced on the file route —
    reading a group- or world-readable secret would quietly normalise
    leaving it readable. Mirrors scripts/verify_apifootball.py."""
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
        return "", KEY_FILE.name, problems
    if mode & 0o077:
        problems.append(
            f"{KEY_FILE} is mode {mode:03o}; it must be 600 (owner-only). "
            f"Refusing to read a group- or world-readable secret.")
        return "", KEY_FILE.name, problems
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        problems.append(f"{KEY_FILE.name} is empty")
    return key, KEY_FILE.name, problems


def _redact(text: object, key: str) -> str:
    out = str(text)
    if key:
        out = out.replace(key, REDACTED)
        if key.strip() and key.strip() != key:
            out = out.replace(key.strip(), REDACTED)
    return out


def scrub_pii(body: object) -> object:
    """Drop the account holder's identity before anything reaches the
    archive. /status returns response.account = {firstname, lastname,
    email}; the first live run of the trial harness put Son's name and email
    into a file destined for the repo."""
    if not isinstance(body, dict):
        return body
    resp = body.get("response")
    if isinstance(resp, dict) and "account" in resp:
        cleaned = dict(resp)
        cleaned["account"] = "<ACCOUNT_HOLDER_PII_DROPPED_BY_PROBE>"
        return dict(body, response=cleaned)
    return body


# --- payload primitives (same discipline as the trial harness) -------------
def response_items(payload: object) -> list:
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response")
    return resp if isinstance(resp, list) else []


def errors_of(payload: object) -> list[str]:
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


def declared_results(payload: object) -> object:
    return payload.get("results") if isinstance(payload, dict) else None


def count_disagreement(payload: object) -> str | None:
    declared, actual = declared_results(payload), len(response_items(payload))
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    if declared != actual:
        return (f"provider 'results' says {declared} but the array holds "
                f"{actual} — the count wins")
    return None


def norm_stat_type(t: object) -> str:
    return "".join(c for c in str(t or "").lower() if c.isalnum())


def is_xg_type(t: object) -> bool:
    n = norm_stat_type(t)
    if not n:
        return False
    if any(bad in n for bad in RULE["xg_type_reject_substrings"]):
        return False
    return (n in set(RULE["xg_type_accept_exact"])
            or RULE["xg_type_accept_substring"] in n)


def parse_number(raw: object) -> float | None:
    """A number, or None. None/''/'-'/'null'/non-numeric are all ABSENCE,
    never zero. This is the single function the whole absent-vs-zero
    discipline rests on."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if not s or s.lower() in {"-", "–", "—", "null", "none",
                              "n/a", "na"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_statistics(payload: object) -> dict:
    """Per-team statistic inventory for one fixture, plus the xG verdict.

    Records the statistic TYPES offered, so "the type is simply not there"
    is a stated fact rather than an inference from a missing value."""
    teams, types_union = [], []
    for entry in response_items(payload):
        if not isinstance(entry, dict):
            continue
        team = entry.get("team") or {}
        stats = entry.get("statistics")
        stats = stats if isinstance(stats, list) else []
        typed = [st for st in stats
                 if isinstance(st, dict) and str(st.get("type") or "").strip()]
        xg_type = xg_raw = None
        xg_value = None
        for st in typed:
            t = st.get("type")
            if str(t) not in types_union:
                types_union.append(str(t))
            if xg_type is None and is_xg_type(t):
                xg_type, xg_raw = str(t), st.get("value")
                xg_value = parse_number(xg_raw)
        teams.append({
            "team_id": team.get("id"),
            "team_name": team.get("name"),
            "statistic_type_count": len(typed),
            "xg_statistic_type": xg_type,
            "xg_raw": xg_raw,
            "xg_value": xg_value,
            # PRESENT means parseable. null is ABSENT, never zero.
            "xg_present": xg_value is not None,
            "xg_zero_valued": xg_value == 0.0 if xg_value is not None
            else False,
        })
    stats_present = any(t["statistic_type_count"] > 0 for t in teams)
    return {
        "entries": len(response_items(payload)),
        "stats_present": stats_present,
        "statistic_types_offered": types_union,
        "statistic_type_count_max": max(
            [t["statistic_type_count"] for t in teams], default=0),
        "teams": teams,
        "xg_present_both_teams": (
            len(teams) == 2 and all(t["xg_present"] for t in teams)),
        "xg_present_any_team": any(t["xg_present"] for t in teams),
        "xg_type_offered": any(t["xg_statistic_type"] for t in teams),
        "xg_zero_valued_any": any(t["xg_zero_valued"] for t in teams),
    }


RED_CARD_TYPES = {"card"}
RED_CARD_DETAILS = {"red card", "second yellow card"}


def read_events(payload: object) -> dict:
    """The timed event feed for one fixture, and whether it carries a
    dismissal. `detail` is what distinguishes a red from a yellow — type is
    'Card' for both, so keying on type alone would call every booking a
    sending-off."""
    items = response_items(payload)
    reds, kinds = [], {}
    for ev in items:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "").strip()
        detail = str(ev.get("detail") or "").strip()
        kinds[f"{etype}/{detail}"] = kinds.get(f"{etype}/{detail}", 0) + 1
        if etype.lower() in RED_CARD_TYPES \
                and detail.lower() in RED_CARD_DETAILS:
            t = ev.get("time") or {}
            reds.append({
                "elapsed": t.get("elapsed"), "extra": t.get("extra"),
                "team": (ev.get("team") or {}).get("name"),
                "team_id": (ev.get("team") or {}).get("id"),
                "detail": detail})
    return {"events_present": len(items) > 0, "event_count": len(items),
            "event_kinds": kinds, "red_cards": reds,
            "red_card_count": len(reds)}


# --- the client ------------------------------------------------------------
class Client:
    """One budgeted, delayed, redacting GET path. No tight retries."""

    def __init__(self, key: str, out: list, say):
        self._key = key
        self.records = out
        self._say = say
        self.used = 0
        self._session = requests.Session()

    def get(self, path: str, params: dict, label: str) -> object:
        if self.used >= HARD_CAP:
            raise BudgetExceeded(
                f"request cap {HARD_CAP} reached before {label} — aborting "
                "rather than spending more of the day's quota")
        if self.used:
            time.sleep(DELAY_SECONDS)
        self.used += 1
        t0 = time.time()
        try:
            r = self._session.get(
                f"{BASE}/{path.lstrip('/')}", params=params,
                headers={"x-apisports-key": self._key},
                timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            err = f"{type(exc).__name__}: {_redact(exc, self._key)}"
            self.records.append({"label": label, "path": path,
                                 "params": params, "transport_error": err})
            raise RuntimeError(f"{label} transport failure: {err}") from None
        elapsed = round((time.time() - t0) * 1000)
        try:
            body = r.json()
        except Exception:
            body = {"_unparseable_body": _redact(r.text[:2000], self._key)}
        rec = {"label": label, "path": path, "params": params,
               "http_status": r.status_code, "elapsed_ms": elapsed,
               "response_array_length": len(response_items(body)),
               "provider_results_field": declared_results(body),
               "provider_errors": errors_of(body),
               "count_disagreement": count_disagreement(body),
               "rate_limit_remaining_minute": r.headers.get(
                   "x-ratelimit-remaining"),
               "rate_limit_remaining_day": r.headers.get(
                   "x-ratelimit-requests-remaining"),
               "body": scrub_pii(body)}
        self.records.append(rec)
        rem = rec["rate_limit_remaining_day"]
        if rem is not None and str(rem).strip() in {"0", "0.0"}:
            raise BudgetExceeded(
                "provider reports 0 daily requests remaining — aborting")
        return body


def sanitise_status(body: object) -> dict:
    resp = body.get("response") if isinstance(body, dict) else None
    if not isinstance(resp, dict):
        return {"unavailable": True}
    return {"subscription": resp.get("subscription"),
            "requests": resp.get("requests"),
            "_account_block_dropped": "account" in resp}


# --- orchestration ---------------------------------------------------------
def probe(key: str, say) -> dict:
    records: list = []
    c = Client(key, records, say)
    out: dict = {"rule": RULE, "run_utc": _now().isoformat(),
                 "min_honest_elapsed": MIN_HONEST_ELAPSED}

    say("── plan / quota ──")
    status = c.get("status", {}, "status")
    out["status"] = sanitise_status(status)
    say(f"    plan: {json.dumps(out['status'].get('subscription'))}")
    say(f"    quota: {json.dumps(out['status'].get('requests'))}")

    say("")
    say("── live fixtures (one request for the whole world) ──")
    live = c.get("fixtures", {"live": "all"}, "fixtures_live_all")
    items = response_items(live)
    disagree = count_disagreement(live)
    out["live_fixtures_total"] = len(items)
    out["live_count_disagreement"] = disagree
    out["live_provider_errors"] = errors_of(live)
    say(f"    live fixtures: {len(items)}")
    if disagree:
        say(f"    !! {disagree}")
    if errors_of(live):
        say(f"    !! provider errors: {errors_of(live)}")

    # group by competition, then sample so the request budget is spread
    # across competitions rather than spent on one busy league
    by_league: dict = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        lg = it.get("league") or {}
        fx = it.get("fixture") or {}
        st = (fx.get("status") or {})
        key_lg = f"{lg.get('id')}|{lg.get('country')}|{lg.get('name')}"
        by_league.setdefault(key_lg, []).append({
            "fixture_id": fx.get("id"),
            "elapsed": st.get("elapsed"),
            "status_short": st.get("short"),
            "league_id": lg.get("id"),
            "league_name": lg.get("name"),
            "league_country": lg.get("country"),
            "home": ((it.get("teams") or {}).get("home") or {}).get("name"),
            "away": ((it.get("teams") or {}).get("away") or {}).get("name"),
            "home_id": ((it.get("teams") or {}).get("home") or {}).get("id"),
            "away_id": ((it.get("teams") or {}).get("away") or {}).get("id"),
            "goals": it.get("goals"),
        })
    out["live_competitions"] = len(by_league)
    say(f"    across {len(by_league)} competitions")

    # deterministic sample: competitions ordered by league id, most-advanced
    # fixture first inside each — a later minute is stronger evidence
    sample: list = []
    for key_lg in sorted(by_league,
                         key=lambda k: str(by_league[k][0]["league_id"])):
        rows = sorted(by_league[key_lg],
                      key=lambda r: -(r["elapsed"] or 0))
        sample.extend(rows[:MAX_PER_LEAGUE])
    sample = sample[:MAX_FIXTURES_PROBED]
    out["fixtures_probed_planned"] = len(sample)

    say("")
    say("── per-fixture statistics + events ──")
    probes: list = []
    for row in sample:
        fid = row["fixture_id"]
        if fid is None:
            continue
        try:
            stats_body = c.get("fixtures/statistics", {"fixture": fid},
                               f"stats_{fid}")
            events_body = c.get("fixtures/events", {"fixture": fid},
                                f"events_{fid}")
        except BudgetExceeded as exc:
            out["budget_abort"] = str(exc)
            say(f"    ABORT: {exc}")
            break
        st = read_statistics(stats_body)
        ev = read_events(events_body)
        elapsed = row["elapsed"]
        early = (elapsed is None or elapsed < MIN_HONEST_ELAPSED)
        verdict = ("stats_present" if st["stats_present"]
                   else "too_early_to_conclude" if early
                   else "stats_absent")
        rec = {**row, "statistics": st, "events": ev,
               "coverage_verdict": verdict,
               "too_early": early,
               "stats_provider_errors": errors_of(stats_body),
               "events_provider_errors": errors_of(events_body)}
        probes.append(rec)
        say(f"    {str(row['league_country'])[:14]:<14} "
            f"{str(row['league_name'])[:26]:<26} "
            f"{str(elapsed) + chr(39):>5} "
            f"{st['statistic_type_count_max']:>3} types  "
            f"{ev['event_count']:>3} events  "
            f"xg={st['teams'][0]['xg_value'] if st['teams'] else None!s:<6} "
            f"{verdict}"
            + (f"  RED×{ev['red_card_count']}"
               if ev["red_card_count"] else ""))
    out["probes"] = probes
    out["requests_used"] = c.used
    out["records"] = records

    # per-competition coverage roll-up: the UNION over probed fixtures,
    # always with its denominator
    cov: dict = {}
    for p in probes:
        k = str(p["league_id"])
        e = cov.setdefault(k, {
            "league_id": p["league_id"], "league_name": p["league_name"],
            "league_country": p["league_country"],
            "fixtures_probed": 0, "elapsed_minutes": [],
            "stats_present": False, "xg_present": False,
            "xg_type_offered": False, "xg_zero_valued": False,
            "events_present": False, "red_cards_seen": 0,
            "max_statistic_types": 0, "too_early_only": True})
        e["fixtures_probed"] += 1
        e["elapsed_minutes"].append(p["elapsed"])
        e["stats_present"] |= p["statistics"]["stats_present"]
        e["xg_present"] |= p["statistics"]["xg_present_any_team"]
        e["xg_type_offered"] |= p["statistics"]["xg_type_offered"]
        e["xg_zero_valued"] |= p["statistics"]["xg_zero_valued_any"]
        e["events_present"] |= p["events"]["events_present"]
        e["red_cards_seen"] += p["events"]["red_card_count"]
        e["max_statistic_types"] = max(
            e["max_statistic_types"],
            p["statistics"]["statistic_type_count_max"])
        if not p["too_early"]:
            e["too_early_only"] = False
    for e in cov.values():
        e["verdict"] = (
            "LIVE_STATS_PRESENT" if e["stats_present"]
            else "TOO_EARLY_TO_CONCLUDE" if e["too_early_only"]
            else "LIVE_STATS_ABSENT")
        e["xg_verdict"] = (
            "XG_PRESENT" if e["xg_present"]
            else "XG_TYPE_OFFERED_BUT_NO_PARSEABLE_VALUE"
            if e["xg_type_offered"] else "XG_ABSENT")
    out["coverage_by_competition"] = cov
    return out


def archive_path(now: datetime, what: str = "coverage") -> Path:
    """Non-clobbering, like the trial harness: a second run on the same UTC
    date gets a suffix rather than destroying the first run's evidence."""
    base = ARCHIVE_DIR / (f"hunter_live_stats_{what}_"
                          f"{now.strftime('%Y-%m-%d')}.json")
    if not base.exists():
        return base
    for n in range(2, 50):
        cand = base.with_suffix(f".run{n}.json")
        if not cand.exists():
            return cand
    raise RuntimeError("too many archives for one date")


def main(argv=None) -> int:
    key, where, problems = load_key()
    lines: list[str] = []

    def say(msg: str = "") -> None:
        text = _redact(msg, key)
        lines.append(text)
        print(text)

    say("API-FOOTBALL LIVE IN-PLAY COVERAGE PROBE")
    say(f"rule version {RULE['version']} registered "
        f"{RULE['registered_utc']} before any live payload was seen")
    say(f"key source: {where}")
    for p in problems:
        say(f"  !! {p}")
    if not key:
        say("")
        say("INDETERMINATE — no key. Not a verdict: nothing was measured.")
        return 2
    now = _now()
    try:
        report = probe(key, say)
    except BudgetExceeded as exc:
        say(f"INDETERMINATE — budget: {_redact(exc, key)}")
        return 2
    except Exception as exc:
        say(f"INDETERMINATE — {type(exc).__name__}: {_redact(exc, key)}")
        return 2

    say("")
    say("── coverage by competition (UNION over probed fixtures) ──")
    cov = report["coverage_by_competition"]
    for k in sorted(cov, key=lambda k: (cov[k]["verdict"],
                                        str(cov[k]["league_country"]))):
        e = cov[k]
        say(f"  [{e['verdict']:<22}] {str(e['league_country'])[:14]:<14} "
            f"{str(e['league_name'])[:28]:<28} "
            f"n={e['fixtures_probed']} at {e['elapsed_minutes']}' "
            f"types<={e['max_statistic_types']} "
            f"{e['xg_verdict']}"
            + (f" reds={e['red_cards_seen']}" if e["red_cards_seen"] else ""))
    say("")
    say(f"requests used: {report['requests_used']} of a {HARD_CAP} cap "
        f"(plan: 7500/day, 300/min)")

    report["stdout"] = lines
    path = archive_path(now)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    blob = _redact(json.dumps(report, indent=1, sort_keys=True,
                              default=str), key)
    path.write_text(blob, encoding="utf-8")
    print(f"archived -> {path.relative_to(REPO_ROOT)} "
          f"({len(blob) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
