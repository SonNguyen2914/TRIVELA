"""API-Football LIVE in-play statistics — the hunter's observational surface.

WHAT THIS IS FOR
================
`hunter.IN_PLAY_OVERREACTION` flags a violent mid-to-mid repricing of one
Kalshi market between two consecutive captures. Until now it could only
say the honest but thin thing: *something may have happened, we cannot
see it.* The conditioning event was UNOBSERVED by construction, so the
finding claimed a move happened and never that the market was wrong.

This module makes that conditioning event OBSERVABLE where the provider's
coverage allows, so a finding can report which of three states it is in:

    conditioning_observed
        an explaining event (goal, red card) was seen in the feed at or
        before the move. The market's reaction is at least partly
        EXPLAINED, which makes it LESS likely to be an overreaction — and
        the finding says so, in those words.
    conditioning_unobserved_stats_available
        the feed was READABLE and shows no explaining event, so the move
        looks less justified. The STRONGEST version of this finding.
    conditioning_unobserved_no_stats
        no readable live feed for this competition. Exactly today's
        behaviour, and it must stay available and honestly labelled —
        most of the world's competitions are here.

THE DIRECTION OF THAT INFERENCE, WRITTEN DOWN BECAUSE INVERTING IT IS THE
DEFECT THIS MODULE IS MOST LIKELY TO GROW
=========================================================================
    FINDING A REASON WEAKENS THE FINDING. IT NEVER STRENGTHENS IT.

A red card makes a 20-cent move MORE reasonable, not less. Code that
treats "we found a cause" as corroboration of a mispricing has the
arithmetic of the world backwards. `CONDITIONING_STRENGTH` below ranks
the three states numerically for exactly one reason: so a test can assert
the ordering mechanically rather than trusting prose.
`tests/test_hunter_live_stats.py::TestInferenceDirectionIsNotInverted`
pins it.

WHY GOALS ARE FIRST-CLASS AND NOT AN AFTERTHOUGHT
=================================================
The obvious explaining event is a red card, and it is the one that gets
talked about. But a GOAL is the overwhelmingly most common cause of a
violent in-play repricing, and goals have BROADER provider coverage than
statistics do (measured: Copa Colombia served 3 goal events with zero
statistic types). A detector that observed red cards and ignored goals
would classify almost every goal-driven move as
`conditioning_unobserved_stats_available` — "the strongest version of
this finding" — which is the inversion above, at scale, on the common
case. Goals are therefore an explaining event of equal standing.

COVERAGE IS MEASURED, NEVER ASSUMED
===================================
Coverage is uneven and undocumented. Measured 2026-07-29 (archived in
`research_archive/hunter_live_stats_coverage_2026-07-29.json`), from 16
live fixtures across 9 competitions:

    LIVE_STATS_PRESENT   CONMEBOL Sudamericana (11)  90'  18 types  xG 1.28
                         Liga Profesional Arg (128)  73'  18 types  xG 0.65
                         Brazil Serie A (71)         59'  18 types  xG 0.73
    LIVE_STATS_ABSENT    Copa Colombia (241) at 90' and 61' — 0 types
                         Goiano-2 (1030), El-Salvador Primera (370),
                         USL Championship (255), USL League One (489)
    TOO_EARLY            EPL Summer Series (1022) at 14'

Two consequences are wired into the types below:

  - **statistics and events coverage are SEPARATE verdicts.** Events are
    strictly broader. One "has live data" flag would have been wrong for
    every competition in the ABSENT list that nonetheless served goals.
  - **absence before `MIN_HONEST_ELAPSED` proves nothing.** A friendly at
    5' has no statistics because nothing has happened yet. That is
    recorded as too-early, never as absence.

ABSENT IS NOT ZERO
==================
`parse_number` returns None for null / '' / '-' / non-numeric, and the
provider serves a present-but-NULL xG for several leagues. A null xG read
as 0.0 would say "this team has created nothing" about a match the
provider simply does not cover. This repo has been burned twice by a
hollow value read as an answer (`results: 0` on a fixture live at the
45th minute; `{"created": 0}` hiding a night of failed writes), so the
distinction is a function, not a convention.

THE LEAKAGE BOUNDARY
====================
Live xG is legitimate in-play evidence AND it carries information from
after a T-10 lock. It belongs to the hunter's observational surface only:
it may reach `HunterFinding.legs_json` and the coverage table, and
NOTHING else. Never a locked artifact, never a `PredictionRun`, never a
paper signal, never a feature the model is fitted or scored on.

`tests/test_hunter_live_stats.py::TestLiveXgCannotReachALockedArtifact`
enforces this two ways: statically, that no module in the lock path names
this one, and behaviourally, that a lock built while a live-xG reading is
in memory contains no trace of it.

THE JOIN IS THE WEAKEST LINK, AND IT SAYS SO
============================================
Attaching a provider fixture to a Kalshi market is a real identity
problem this repo has NOT solved. `docs/APIFOOTBALL-TRIAL.md` §8.6 says
so explicitly: CHECK 3 resolved team NAMES, not fixtures, and the rule
that fixture identity must never come from names plus a date still
stands.

So the join here is a HEURISTIC, and every finding it produces carries
its basis verbatim. It is built to fail closed:

  - the series -> league pairing is a frozen, FINGERPRINTED table
    (`COMPETITION_MAP`), because a reference table anyone can extend is a
    reference table that gets extended to make a stubborn case resolve —
    which is precisely how the trial harness's own frozen roster was
    tuned on day one (see amendment A2);
  - team codes come from the Kalshi LEG SUFFIXES, not from splitting the
    concatenated event ticker: `...-26JUL30CCTUC` is unsplittable without
    a mapping, but its legs are `-TIE`, `-CC`, `-TUC`, so the provider
    hands us the two codes separately;
  - BOTH codes must resolve to the two sides of exactly ONE live fixture.
    Zero matches, two matches, or an ambiguous code is `join_unresolved`
    — never a silent pick. An ambiguous identity match fails explicitly
    (AGENTS.md §13).

A wrong join is the worst failure available to this module: it would
attach another match's red card to this market, and report
`conditioning_observed` — "the market had a reason" — about a market
where nothing happened. That is why refusing is cheap here and guessing
is not.

MONEY
=====
Observational. No order path, no sizing, no advice. Money stays locked.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config

# --------------------------------------------------------------------------
# the three conditioning states, and the ONE ordering that matters
# --------------------------------------------------------------------------
CONDITIONING_OBSERVED = "conditioning_observed"
CONDITIONING_UNOBSERVED_STATS_AVAILABLE = (
    "conditioning_unobserved_stats_available")
CONDITIONING_UNOBSERVED_NO_STATS = "conditioning_unobserved_no_stats"

CONDITIONING_STATES = (CONDITIONING_OBSERVED,
                       CONDITIONING_UNOBSERVED_STATS_AVAILABLE,
                       CONDITIONING_UNOBSERVED_NO_STATS)

# How much each state supports reading the move as an OVERREACTION.
#
# This exists to be asserted, not admired. The ordering is the whole
# inference and it is the thing a future edit is most likely to get
# backwards, so it lives in one numeric table a test can check instead of
# being spread across prose that reads fine either way.
#
#   observed            an explaining event was SEEN. The move has a cause,
#                       so it is the WEAKEST support for "the market is
#                       wrong" — finding a reason weakens the finding.
#   unobserved+stats    the feed was readable and showed no cause. The
#                       STRONGEST support available here.
#   unobserved+no stats we could not look. Not evidence either way, and
#                       never allowed to read as clean.
CONDITIONING_STRENGTH = {
    CONDITIONING_OBSERVED: 0,
    CONDITIONING_UNOBSERVED_NO_STATS: 1,
    CONDITIONING_UNOBSERVED_STATS_AVAILABLE: 2,
}

CONDITIONING_NOTE = {
    CONDITIONING_OBSERVED: (
        "an explaining match event was OBSERVED in the provider feed at or "
        "before this price move, so the market's reaction is at least "
        "partly EXPLAINED. That makes it LESS likely to be an "
        "overreaction, not more — a move with a visible cause is a move "
        "with a reason. This row is weaker evidence of mispricing than a "
        "move with no visible cause, and it remains an observation either "
        "way: nothing here claims the market is wrong"),
    CONDITIONING_UNOBSERVED_STATS_AVAILABLE: (
        "the provider feed was READABLE for this fixture and shows NO "
        "explaining event at or before the move, so the repricing looks "
        "less justified by observable match events. This is the strongest "
        "version of this finding — and it is still an observation, not a "
        "claim that the market is wrong: the feed can miss things "
        "(a tactical collapse, an injury, a keeper error short of a goal) "
        "and the market may be pricing information this scanner has no "
        "access to"),
    CONDITIONING_UNOBSERVED_NO_STATS: (
        "no readable live feed for this competition, so the conditioning "
        "event is UNOBSERVED — exactly as it has always been for this "
        "detector. This is NOT evidence that nothing happened, and it is "
        "NOT a weaker anomaly: it is an absence of evidence, recorded as "
        "one. 'We could not look' is never 'we looked and saw nothing'"),
}

# --------------------------------------------------------------------------
# coverage verdicts
# --------------------------------------------------------------------------
COVERAGE_PRESENT = "LIVE_STATS_PRESENT"
COVERAGE_ABSENT = "LIVE_STATS_ABSENT"
COVERAGE_TOO_EARLY = "TOO_EARLY_TO_CONCLUDE"
COVERAGE_UNMEASURED = "UNMEASURED"

# Below this many elapsed minutes an empty statistics payload proves
# nothing — measured: a friendly at 5' and an EPL Summer Series fixture at
# 14' both served zero statistic types, and a CONMEBOL fixture at 90'
# served 18. Absence needs the match to have had time to generate one.
MIN_HONEST_ELAPSED = 20

XG_TYPE_REJECT = ("against", "conceded", "prevented", "allowed",
                  "opponent", "xga")
XG_TYPE_ACCEPT_EXACT = ("xg",)
XG_TYPE_ACCEPT_SUBSTRING = "expectedgoals"

# --------------------------------------------------------------------------
# the explaining-event vocabulary — MEASURED, not read off documentation
# --------------------------------------------------------------------------
# `fixtures/events` reports a booking and a sending-off with the SAME
# type ('Card'); only `detail` separates them. A matcher keyed on a
# guessed string would never fire, and every red-carded match would then
# land in `conditioning_unobserved_stats_available` — the strongest
# state. So the strings were measured over 18 completed fixtures in the
# three covered leagues (archived in
# research_archive/hunter_live_stats_redcard_strings_2026-07-29.json):
#
#       68x  'Card / Yellow Card'
#        2x  'Card / Red Card'        <-- CONFIRMED verbatim
#        2x  'Var / Card upgrade'     <-- accompanied a red BOTH times
#
# 'Second Yellow card' was NOT observed in that sample (68 yellows, zero
# second-yellows). It is included below anyway and flagged UNVERIFIED,
# because the asymmetry of the mistake is not symmetric: matching a
# string the provider never sends costs nothing, while FAILING to match a
# real dismissal pushes that fixture into the strongest finding state.
# When in doubt, over-detect the explanation.
RED_CARD_DETAILS_MEASURED = ("red card",)
RED_CARD_DETAILS_UNVERIFIED = ("second yellow card", "secondyellow")
RED_CARD_STRING_PROVENANCE = {
    "measured": list(RED_CARD_DETAILS_MEASURED),
    "measured_on": "2026-07-29",
    "measured_over": ("18 completed fixtures in leagues 11 / 128 / 71; "
                      "68 'Card / Yellow Card', 2 'Card / Red Card', "
                      "2 'Var / Card upgrade'"),
    "unverified_but_matched_defensively": list(RED_CARD_DETAILS_UNVERIFIED),
    "why_defensive": ("a dismissal we fail to recognise becomes "
                      "'no explaining event', which is the STRONGEST "
                      "finding state — so an unmatched real red card "
                      "inverts the inference. Matching a string the "
                      "provider never sends costs nothing"),
    "var_card_upgrade": ("'Var / Card upgrade' is CORROBORATION, not an "
                         "independent dismissal: measured once at 61' one "
                         "minute BEFORE the red at 62' and once at 90' "
                         "alongside another. Counting it separately would "
                         "double-count one sending-off"),
    "archive": ("research_archive/"
                "hunter_live_stats_redcard_strings_2026-07-29.json"),
}

GOAL_TYPE = "goal"
# 'Missed Penalty' is type 'Goal' in this provider's taxonomy and is NOT a
# goal. Cancelled VAR goals are likewise not goals. Both were observed.
GOAL_DETAIL_REJECT = ("missed", "cancelled", "canceled", "disallowed")


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------
KEY_FILE = Path.home() / ".apifootball_key"


def load_key() -> tuple[str, str]:
    """(key, where_it_came_from). Empty key means this module is INERT.

    `config.APIFOOTBALL_KEY` wins (that is the container route). The
    `~/.apifootball_key` fallback exists for the developer machine and
    must be mode 600 — reading a group- or world-readable secret would
    quietly normalise leaving it readable.

    The legacy `config.API_FOOTBALL_KEY` is deliberately NOT consulted:
    it may still hold the free-tier key, which was measured season-blind
    for current seasons, and a confident answer about the wrong plan is
    the failure this whole trial exists to avoid.
    """
    if config.APIFOOTBALL_KEY:
        return config.APIFOOTBALL_KEY, "APIFOOTBALL_KEY"
    try:
        if not KEY_FILE.exists():
            return "", "nowhere"
        if KEY_FILE.stat().st_mode & 0o077:
            print(f"[apifootball_live] {KEY_FILE.name} is group/world "
                  "readable; refusing to read it. chmod 600 it.")
            return "", "refused_bad_mode"
        return KEY_FILE.read_text(encoding="utf-8").strip(), KEY_FILE.name
    except OSError as exc:
        print(f"[apifootball_live] key file unreadable: {type(exc).__name__}")
        return "", "unreadable"


def redact(text: object, key: str | None = None) -> str:
    """Strip the key from anything on its way to a log or a stored row.

    The key travels in a header and is never placed in a URL or a param,
    but a provider error string or a `requests` exception can echo one
    back — and `requests` exceptions routinely embed the request URL.
    """
    out = str(text)
    k = key if key is not None else config.APIFOOTBALL_KEY
    if k:
        out = out.replace(k, "<APIFOOTBALL_KEY_REDACTED>")
        if k.strip() and k.strip() != k:
            out = out.replace(k.strip(), "<APIFOOTBALL_KEY_REDACTED>")
    return out


# --------------------------------------------------------------------------
# the frozen series -> league table
# --------------------------------------------------------------------------
# FINGERPRINTED ON PURPOSE. The trial harness's frozen ESPN roster was
# edited, within an hour of first running, to contain exactly the two
# names that had just failed — flipping a DROP into a KEEP under an
# unchanged criteria fingerprint (docs/APIFOOTBALL-TRIAL.md §7/A2). The
# lesson generalises to any reference table a resolver consults: if it can
# be extended quietly, it will be extended to make a stubborn case
# resolve, and then the resolution rate means nothing.
#
# So: every entry states how the pairing was ESTABLISHED, the whole table
# is hashed, and `competition_map_fingerprint()` is recorded on every
# finding this module contributes to. Adding a pairing changes the hash,
# which puts the change in the diff.
#
# Only pairings backed by evidence are here. The three leagues MEASURED to
# carry live statistics on 2026-07-29, each paired with the Kalshi series
# that exists for that competition in the taxonomy snapshot
# (research_archive/kalshi_soccer_taxonomy_2026-07-28.json). Everything
# else is deliberately absent: an unmapped series yields
# `conditioning_unobserved_no_stats`, which is honest and is today's
# behaviour, whereas a guessed pairing risks the worst failure this module
# has (a wrong fixture, hence another match's red card on this market).
COMPETITION_MAP = {
    "KXCONMEBOLSUDGAME": {
        "league_id": 11,
        "league_name": "CONMEBOL Sudamericana",
        "basis": ("league id and live-statistics coverage MEASURED "
                  "2026-07-29 (90', 18 statistic types, xG 1.28); Kalshi "
                  "series present in the 2026-07-28 taxonomy snapshot"),
    },
    "KXARGPREMDIVGAME": {
        "league_id": 128,
        "league_name": "Liga Profesional Argentina",
        "basis": ("league id and live-statistics coverage MEASURED "
                  "2026-07-29 (73', 18 statistic types, xG 0.65); Kalshi "
                  "series present with 36 open markets in the 2026-07-28 "
                  "taxonomy snapshot"),
    },
    "KXBRASILEIROGAME": {
        "league_id": 71,
        "league_name": "Brazil Serie A",
        "basis": ("league id and live-statistics coverage MEASURED "
                  "2026-07-29 (59' and 58', 18 statistic types, xG 0.73 "
                  "and 1.03); Kalshi series present in the 2026-07-28 "
                  "taxonomy snapshot"),
    },
}

COMPETITION_MAP_NOTE = (
    "series -> API-Football league pairings, frozen and fingerprinted. Only "
    "pairings whose league id AND live-statistics coverage were measured "
    "are present; an unmapped series yields conditioning_unobserved_no_stats "
    "rather than a guessed league. Extending this table changes the "
    "fingerprint recorded on every finding")


def competition_map_fingerprint() -> str:
    """sha256 of the frozen table, recorded on every finding built from
    it. A quiet extension is therefore visible in the stored evidence, not
    only in the diff."""
    blob = json.dumps(COMPETITION_MAP, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# payload primitives — pure, and the whole absent-vs-zero discipline
# --------------------------------------------------------------------------
def response_items(payload: object) -> list:
    """The elements of `response`, or []. Never None, and never a bare
    truthiness test on the payload."""
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response")
    return resp if isinstance(resp, list) else []


def errors_of(payload: object) -> list[str]:
    """API-Football reports plan/token/parameter problems INSIDE a 200
    body, as `errors` — a dict when populated and [] when not."""
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


def count_disagreement(payload: object) -> str | None:
    """The provider's own `results` field against the length of the array
    it shipped. `results: 0` beside a populated array — or the reverse —
    is the exact shape of the 2026-07-09 incident, so it is always
    reported and the COUNT always wins."""
    if not isinstance(payload, dict):
        return None
    declared = payload.get("results")
    actual = len(response_items(payload))
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    if declared != actual:
        return (f"provider 'results' says {declared} but the array holds "
                f"{actual} — the count wins")
    return None


def parse_number(raw: object) -> float | None:
    """A number, or None.

    THE function the absent-vs-zero discipline rests on. None, '', '-',
    'null' and any non-numeric string are ABSENCE and return None — never
    0.0. API-Football serves a present-but-NULL xG for several leagues
    (Bundesliga, J1, Primeira, measured), and a null read as zero says
    "this team created nothing" about a competition the provider simply
    does not cover.
    """
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


def norm_stat_type(t: object) -> str:
    return "".join(c for c in str(t or "").lower() if c.isalnum())


def is_xg_type(t: object) -> bool:
    """An expected-goals FOR figure. Rejects the against/conceded/
    prevented variants: xGA is a different statistic, and accepting it
    would let this module read the OPPONENT's number as this team's."""
    n = norm_stat_type(t)
    if not n:
        return False
    if any(bad in n for bad in XG_TYPE_REJECT):
        return False
    return n in XG_TYPE_ACCEPT_EXACT or XG_TYPE_ACCEPT_SUBSTRING in n


def _norm_name(name: object) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " "
                            for c in s.lower()).split())


def read_statistics(payload: object) -> dict:
    """One fixture's per-team statistic inventory, plus the xG verdict.

    Records the statistic TYPES the provider offered, so "the type is
    simply not there" is a stated fact rather than an inference from a
    missing value.
    """
    teams, types_union = [], []
    for entry in response_items(payload):
        if not isinstance(entry, dict):
            continue
        team = entry.get("team") or {}
        stats = entry.get("statistics")
        stats = stats if isinstance(stats, list) else []
        typed = [st for st in stats if isinstance(st, dict)
                 and str(st.get("type") or "").strip()]
        xg_type = xg_raw = None
        xg_value = None
        shots = shots_on = possession = None
        for st in typed:
            t = st.get("type")
            if str(t) not in types_union:
                types_union.append(str(t))
            n = norm_stat_type(t)
            if xg_type is None and is_xg_type(t):
                xg_type, xg_raw = str(t), st.get("value")
                xg_value = parse_number(xg_raw)
            elif n == "totalshots":
                shots = parse_number(st.get("value"))
            elif n == "shotsongoal":
                shots_on = parse_number(st.get("value"))
            elif n == "ballpossession":
                possession = parse_number(st.get("value"))
        teams.append({
            "team_id": team.get("id"),
            "team_name": team.get("name"),
            "statistic_type_count": len(typed),
            "xg_statistic_type": xg_type,
            "xg_raw": xg_raw,
            "xg_value": xg_value,
            # PRESENT means PARSEABLE. A null is absent, never zero.
            "xg_present": xg_value is not None,
            "xg_zero_valued": xg_value == 0.0 if xg_value is not None
            else False,
            "total_shots": shots,
            "shots_on_goal": shots_on,
            "ball_possession_pct": possession,
        })
    return {
        "entries": len(response_items(payload)),
        "stats_present": any(t["statistic_type_count"] > 0 for t in teams),
        "statistic_types_offered": types_union,
        "statistic_type_count_max": max(
            [t["statistic_type_count"] for t in teams], default=0),
        "teams": teams,
        "xg_present_both_teams": (len(teams) == 2
                                  and all(t["xg_present"] for t in teams)),
        "xg_present_any_team": any(t["xg_present"] for t in teams),
        "xg_type_offered": any(t["xg_statistic_type"] for t in teams),
        "provider_errors": errors_of(payload),
        "count_disagreement": count_disagreement(payload),
    }


def _is_red_card(etype: str, detail: str) -> tuple[bool, bool]:
    """(is_dismissal, was_matched_on_an_unverified_string).

    Only `detail` separates a booking from a sending-off — `type` is
    'Card' for both. The second element exists so a finding can say which
    of its dismissals rests on a string this repo has actually seen.
    """
    if etype.strip().lower() != "card":
        return False, False
    d = _norm_name(detail).replace(" ", "")
    for measured in RED_CARD_DETAILS_MEASURED:
        if _norm_name(measured).replace(" ", "") == d:
            return True, False
    for guess in RED_CARD_DETAILS_UNVERIFIED:
        if _norm_name(guess).replace(" ", "") in d:
            return True, True
    return False, False


def _is_goal(etype: str, detail: str) -> bool:
    """A goal actually scored. 'Missed Penalty' carries type 'Goal' in
    this provider's taxonomy and is not a goal; a VAR-cancelled goal is
    not a goal either. Both were observed in the measured sample."""
    if _norm_name(etype).replace(" ", "") != GOAL_TYPE:
        return False
    d = _norm_name(detail)
    return not any(bad in d for bad in GOAL_DETAIL_REJECT)


def read_events(payload: object) -> dict:
    """One fixture's timed event feed, and the explaining events in it.

    `events_present` is `len(response) > 0` — and an EMPTY array is
    treated as "we could not see", never as "nothing happened". That
    distinction is load-bearing downstream: absence of a red card in a
    feed that carries no events is not evidence there was no red card,
    and claiming otherwise is the `results: 0` mistake.
    """
    items = response_items(payload)
    kinds: dict[str, int] = {}
    goals, reds = [], []
    unverified_match = False
    for ev in items:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "").strip()
        detail = str(ev.get("detail") or "").strip()
        kinds[f"{etype}/{detail}"] = kinds.get(f"{etype}/{detail}", 0) + 1
        t = ev.get("time") or {}
        team = ev.get("team") or {}
        base = {"elapsed": t.get("elapsed"), "extra": t.get("extra"),
                "team_id": team.get("id"), "team_name": team.get("name"),
                "type": etype, "detail": detail}
        is_red, unverified = _is_red_card(etype, detail)
        if is_red:
            reds.append({**base, "matched_on_unverified_string": unverified})
            unverified_match = unverified_match or unverified
        elif _is_goal(etype, detail):
            goals.append(base)
    return {
        "events_present": len(items) > 0,
        "event_count": len(items),
        "event_kinds": kinds,
        "goals": goals,
        "red_cards": reds,
        "goal_count": len(goals),
        "red_card_count": len(reds),
        "any_dismissal_matched_on_unverified_string": unverified_match,
        "provider_errors": errors_of(payload),
        "count_disagreement": count_disagreement(payload),
        "empty_feed_note": (
            None if items else
            "the events array is EMPTY: this is 'we could not see', NOT "
            "'nothing happened'. A competition with no event coverage and "
            "a genuinely uneventful match are indistinguishable here, so "
            "no explaining event may be ruled OUT from this"),
    }


def coverage_verdict(stats: dict, elapsed: object) -> str:
    """PRESENT / ABSENT / TOO_EARLY for one reading.

    Absence below MIN_HONEST_ELAPSED is TOO_EARLY, never ABSENT: measured,
    a fixture at 14' served zero statistic types and one at 90' served 18.
    """
    if stats.get("stats_present"):
        return COVERAGE_PRESENT
    mins = parse_number(elapsed)
    if mins is None or mins < MIN_HONEST_ELAPSED:
        return COVERAGE_TOO_EARLY
    return COVERAGE_ABSENT


# --------------------------------------------------------------------------
# the budgeted client
# --------------------------------------------------------------------------
_MIN_GAP_S = 0.35
_last_call = 0.0
# whole-process daily accounting, against OUR clock's UTC day
_day_spend: dict = {"date": None, "count": 0}


class BudgetExceeded(RuntimeError):
    pass


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_spend() -> dict:
    """Requests spent today by this process, and the ceiling. Exposed so
    the read surface can show it rather than leaving quota invisible."""
    if _day_spend["date"] != _utc_date():
        return {"date": _utc_date(), "count": 0,
                "cap": config.HUNTER_LIVE_STATS_MAX_REQUESTS_PER_DAY}
    return {"date": _day_spend["date"], "count": _day_spend["count"],
            "cap": config.HUNTER_LIVE_STATS_MAX_REQUESTS_PER_DAY}


def _reset_day_spend_for_tests() -> None:
    """Test-only. No runtime caller, no route."""
    _day_spend["date"] = None
    _day_spend["count"] = 0


def _spend_one() -> None:
    today = _utc_date()
    if _day_spend["date"] != today:
        _day_spend["date"] = today
        _day_spend["count"] = 0
    cap = config.HUNTER_LIVE_STATS_MAX_REQUESTS_PER_DAY
    if _day_spend["count"] >= cap:
        raise BudgetExceeded(
            f"daily cap {cap} reached ({_day_spend['count']} spent on "
            f"{today}) — refusing to spend more of the plan's quota")
    _day_spend["count"] += 1


class LiveClient:
    """One throttled, budgeted, redacting GET path for the live endpoints.

    Per-cycle cap AND a whole-process daily cap. Never a tight retry: a
    failure is recorded and the cycle carries on with less evidence,
    because a scanner that hammers a metered provider gets its key
    throttled and then observes nothing at all.
    """

    def __init__(self, key: str, cycle_cap: int | None = None):
        self._key = key
        self.cycle_cap = (cycle_cap if cycle_cap is not None
                          else config.HUNTER_LIVE_STATS_MAX_REQUESTS_PER_CYCLE)
        self.used = 0
        self.failures: list[dict] = []
        self.quota_remaining_day = None
        self.quota_remaining_minute = None

    def get(self, path: str, params: dict, label: str) -> tuple[object,
                                                                datetime]:
        """(payload, captured_at). `captured_at` is OUR fetch-COMPLETION
        clock — the same discipline the Kalshi side uses, and for the same
        reason: the provider publishes no reading timestamp, so our clock
        is the only timing evidence the reading gets."""
        global _last_call
        if self.used >= self.cycle_cap:
            raise BudgetExceeded(
                f"per-cycle cap {self.cycle_cap} reached before {label}")
        _spend_one()
        wait = _MIN_GAP_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        self.used += 1
        try:
            r = requests.get(
                f"{config.APIFOOTBALL_BASE.rstrip('/')}/{path.lstrip('/')}",
                params=params, headers={"x-apisports-key": self._key},
                timeout=15)
        except requests.RequestException as exc:
            # the CLASS only, never str(exc): a requests exception embeds
            # the request URL, and redact() is belt-and-braces on top
            self.failures.append({"label": label,
                                  "error": type(exc).__name__})
            print(f"[apifootball_live] {label} transport failed: "
                  f"{type(exc).__name__}")
            raise
        captured_at = datetime.now(timezone.utc)
        self.quota_remaining_day = r.headers.get(
            "x-ratelimit-requests-remaining")
        self.quota_remaining_minute = r.headers.get("x-ratelimit-remaining")
        try:
            body = r.json()
        except ValueError:
            self.failures.append({"label": label,
                                  "error": "unparseable_body",
                                  "http_status": r.status_code})
            print(f"[apifootball_live] {label} returned an unparseable "
                  f"body (HTTP {r.status_code})")
            return {}, captured_at
        errs = errors_of(body)
        if errs:
            # a 200 carrying `errors` is a FAILURE, not data
            self.failures.append({"label": label,
                                  "provider_errors": [redact(e, self._key)
                                                      for e in errs],
                                  "http_status": r.status_code})
            print(f"[apifootball_live] {label} provider errors: "
                  f"{[redact(e, self._key) for e in errs]}")
        return body, captured_at

    def budget_block(self) -> dict:
        return {
            "requests_used_this_cycle": self.used,
            "cycle_cap": self.cycle_cap,
            "day": day_spend(),
            "provider_quota_remaining_day": self.quota_remaining_day,
            "provider_quota_remaining_minute": self.quota_remaining_minute,
            "failures": self.failures,
            "plan_limits": "API-Football PRO: 300/min, 7500/day",
            "note": ("a cycle spends ZERO requests unless an "
                     "overreaction candidate exists; when one does it "
                     "pays 1 for the whole world's live fixtures plus 2 "
                     "per distinct RESOLVED fixture"),
        }


# --------------------------------------------------------------------------
# live fixture index + the fail-closed join
# --------------------------------------------------------------------------
def fetch_live_index(client: LiveClient) -> dict:
    """Every in-play fixture in the world, for the price of ONE request.

    This is why the module does not sweep: one call enumerates the
    candidate pool, and only fixtures that a Kalshi market actually
    resolves to cost anything more.
    """
    body, captured_at = client.get("fixtures", {"live": "all"}, "live_all")
    by_league: dict[int, list[dict]] = {}
    total = 0
    for it in response_items(body):
        if not isinstance(it, dict):
            continue
        lg = it.get("league") or {}
        fx = it.get("fixture") or {}
        st = fx.get("status") or {}
        teams = it.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        lid = lg.get("id")
        if fx.get("id") is None or lid is None:
            continue          # no provider-stable identity, no candidate
        total += 1
        by_league.setdefault(int(lid), []).append({
            "fixture_id": fx.get("id"),
            "league_id": int(lid),
            "league_name": lg.get("name"),
            "league_country": lg.get("country"),
            "kickoff_utc": fx.get("date"),
            "elapsed": st.get("elapsed"),
            "status_short": st.get("short"),
            "home_id": home.get("id"), "home_name": home.get("name"),
            "away_id": away.get("id"), "away_name": away.get("name"),
            "goals": it.get("goals"),
        })
    return {"captured_at": captured_at, "by_league": by_league,
            "fixtures_live_total": total,
            "count_disagreement": count_disagreement(body),
            "provider_errors": errors_of(body)}


def _code_matches_name(code: str, name: object) -> bool:
    """Does a Kalshi 2-4 letter team code plausibly denote this team?

    Three signals, all conservative:
      - the code is a prefix of one significant token ('TUC' -> 'tucuman')
      - the code equals the initials of the significant tokens
        ('CC' -> 'central cordoba')
      - the code is a prefix of the whole name with spaces removed

    This is a HEURISTIC and the caller treats it as one: a match is only
    ever accepted when it is UNIQUE across the candidate fixtures, and any
    ambiguity is refused rather than resolved.
    """
    c = _norm_name(code).replace(" ", "")
    n = _norm_name(name)
    if not c or not n:
        return False
    tokens = [t for t in n.split() if len(t) > 2]
    if any(t.startswith(c) for t in tokens):
        return True
    if tokens and c == "".join(t[0] for t in tokens):
        return True
    return n.replace(" ", "").startswith(c)


def team_codes_from_tickers(market_tickers: list[str]) -> list[str]:
    """The two team codes for one Kalshi event, taken from its LEG
    SUFFIXES.

    `KXARGPREMDIVGAME-26JUL30CCTUC` cannot be split into 'CC' and 'TUC'
    without already knowing the answer — but the event's legs are
    `-TIE`, `-CC` and `-TUC`, so the provider hands the two codes over
    separately. Reading them here rather than parsing the concatenation is
    the difference between a defined operation and a guess.
    """
    codes = []
    for t in market_tickers or []:
        suffix = (t or "").rsplit("-", 1)[-1].strip().upper()
        if not suffix or suffix in ("TIE", "DRAW"):
            continue
        if suffix not in codes:
            codes.append(suffix)
    return codes


JOIN_RESOLVED = "resolved"
JOIN_NO_COMPETITION_MAP = "unresolved_series_not_mapped"
JOIN_NO_LIVE_FIXTURE = "unresolved_no_live_fixture_in_competition"
JOIN_NO_CODES = "unresolved_no_team_codes_on_event"
JOIN_AMBIGUOUS = "unresolved_ambiguous"
JOIN_NO_MATCH = "unresolved_no_candidate_matched"

JOIN_BASIS_NOTE = (
    "HEURISTIC JOIN, and the weakest link in this evidence chain. Kalshi "
    "team CODES (from the event's leg suffixes, never from splitting the "
    "concatenated ticker) are matched against the provider's team NAMES "
    "among the fixtures currently live in the mapped competition. Fixture "
    "identity from names is exactly what AGENTS.md §13 forbids for a "
    "MODEL feature, and docs/APIFOOTBALL-TRIAL.md §8.6 records that "
    "fixture-level identity is unproven in this repo — so this join feeds "
    "the OBSERVATIONAL surface only, is required to be UNIQUE, and refuses "
    "on any ambiguity instead of picking. A wrong join would attach "
    "another match's red card to this market and report the market's move "
    "as EXPLAINED when nothing happened")


def resolve_fixture(series: str, market_tickers: list[str],
                    live_index: dict) -> dict:
    """Attach ONE live provider fixture to one Kalshi event, or refuse.

    Fail-closed at every step, and the returned block always states which
    step it stopped at — an unresolved join is recorded evidence, not a
    silent absence.
    """
    out = {"status": None, "basis": JOIN_BASIS_NOTE,
           "competition_map_fingerprint": competition_map_fingerprint(),
           "series": series}
    mapped = COMPETITION_MAP.get((series or "").upper())
    if not mapped:
        out["status"] = JOIN_NO_COMPETITION_MAP
        out["detail"] = (
            f"{series} has no measured API-Football league pairing. Only "
            f"{sorted(COMPETITION_MAP)} are mapped, each on measured "
            "evidence; an unmapped series is NOT joined on a guess")
        return out
    out["league_id"] = mapped["league_id"]
    out["league_name"] = mapped["league_name"]
    out["competition_map_basis"] = mapped["basis"]
    candidates = (live_index.get("by_league") or {}).get(
        mapped["league_id"]) or []
    out["live_fixtures_in_competition"] = len(candidates)
    if not candidates:
        out["status"] = JOIN_NO_LIVE_FIXTURE
        out["detail"] = ("no fixture is currently live in this "
                         "competition, so the market's move cannot be "
                         "attributed to an observed match")
        return out
    codes = team_codes_from_tickers(market_tickers)
    out["kalshi_team_codes"] = codes
    if len(codes) != 2:
        out["status"] = JOIN_NO_CODES
        out["detail"] = (
            f"expected exactly 2 non-TIE leg suffixes to read team codes "
            f"from, got {len(codes)}: {codes}. Without both codes the "
            "fixture cannot be pinned and is not guessed")
        return out
    hits = []
    for fx in candidates:
        a, b = codes
        forward = (_code_matches_name(a, fx["home_name"])
                   and _code_matches_name(b, fx["away_name"]))
        reverse = (_code_matches_name(a, fx["away_name"])
                   and _code_matches_name(b, fx["home_name"]))
        if forward or reverse:
            hits.append({**fx, "orientation": "home_away" if forward
                         else "away_home"})
    out["candidates_matched"] = len(hits)
    if len(hits) > 1:
        out["status"] = JOIN_AMBIGUOUS
        out["detail"] = (
            f"{len(hits)} live fixtures matched codes {codes}: "
            f"{[(h['home_name'], h['away_name']) for h in hits]}. An "
            "ambiguous identity match FAILS EXPLICITLY — nothing is "
            "silently picked (AGENTS.md §13)")
        return out
    if not hits:
        out["status"] = JOIN_NO_MATCH
        out["detail"] = (
            f"codes {codes} matched none of the {len(candidates)} live "
            f"fixtures in {mapped['league_name']}: "
            f"{[(c['home_name'], c['away_name']) for c in candidates]}")
        return out
    fx = hits[0]
    out["status"] = JOIN_RESOLVED
    out["fixture_id"] = fx["fixture_id"]
    out["home_name"] = fx["home_name"]
    out["away_name"] = fx["away_name"]
    out["home_id"] = fx["home_id"]
    out["away_id"] = fx["away_id"]
    out["orientation"] = fx["orientation"]
    out["elapsed"] = fx["elapsed"]
    out["status_short"] = fx["status_short"]
    out["goals"] = fx["goals"]
    out["kickoff_utc"] = fx["kickoff_utc"]
    return out


# --------------------------------------------------------------------------
# one fixture's live reading
# --------------------------------------------------------------------------
def fetch_reading(client: LiveClient, fixture_id: object) -> dict:
    """Statistics + events for one resolved fixture, each with OUR capture
    clock. 2 requests. A failure on either leg is recorded and the reading
    reports what it could not read, rather than reporting an absence it
    did not establish."""
    out: dict = {"fixture_id": fixture_id,
                 "capture_clock_note": (
                     "every timestamp here is OUR fetch-completion clock; "
                     "API-Football publishes no reading timestamp, the same "
                     "condition as Kalshi's missing quote timestamp")}
    try:
        stats_body, stats_at = client.get(
            "fixtures/statistics", {"fixture": fixture_id},
            f"stats_{fixture_id}")
        out["statistics"] = read_statistics(stats_body)
        out["statistics_captured_at"] = stats_at.isoformat()
    except (requests.RequestException, BudgetExceeded) as exc:
        out["statistics"] = None
        out["statistics_error"] = redact(f"{type(exc).__name__}: {exc}")
    try:
        events_body, events_at = client.get(
            "fixtures/events", {"fixture": fixture_id},
            f"events_{fixture_id}")
        out["events"] = read_events(events_body)
        out["events_captured_at"] = events_at.isoformat()
    except (requests.RequestException, BudgetExceeded) as exc:
        out["events"] = None
        out["events_error"] = redact(f"{type(exc).__name__}: {exc}")
    return out


# --------------------------------------------------------------------------
# the conditioning classification — the point of the whole module
# --------------------------------------------------------------------------
def classify_conditioning(reading: dict | None, join: dict | None,
                          elapsed: object = None,
                          window_minutes: float | None = None) -> dict:
    """Which of the three conditioning states this move is in, and why.

    Ordering of the tests matters and is deliberate:

      1. an OBSERVED explaining event wins outright. It weakens the
         finding, and a weakening fact must never be outranked by a
         strengthening one.
      2. the strong "no cause visible" state requires that a cause COULD
         have been seen: statistics readable AND a non-empty event feed.
         An empty event feed is 'we could not see', so absence of a red
         card in it rules nothing out.
      3. everything else is the honest no-feed state — today's behaviour.
    """
    out: dict = {
        "states": list(CONDITIONING_STATES),
        "inference_direction": (
            "an OBSERVED conditioning event makes the market's move MORE "
            "reasonable and this finding WEAKER. Finding a reason never "
            "strengthens the finding"),
        "join": join,
    }
    if join is None or join.get("status") != JOIN_RESOLVED:
        out["state"] = CONDITIONING_UNOBSERVED_NO_STATS
        out["reason"] = (
            "no provider fixture could be joined to this Kalshi market"
            + (f" ({join.get('status')})" if join else ""))
        out["note"] = CONDITIONING_NOTE[out["state"]]
        out["strength_rank"] = CONDITIONING_STRENGTH[out["state"]]
        return out
    if reading is None:
        out["state"] = CONDITIONING_UNOBSERVED_NO_STATS
        out["reason"] = "the fixture resolved but no reading was taken"
        out["note"] = CONDITIONING_NOTE[out["state"]]
        out["strength_rank"] = CONDITIONING_STRENGTH[out["state"]]
        return out

    stats = reading.get("statistics")
    events = reading.get("events")
    stats_readable = bool(stats and stats.get("stats_present"))
    events_readable = bool(events and events.get("events_present"))
    cov = coverage_verdict(stats or {}, elapsed)
    out["coverage_verdict"] = cov
    out["statistics_readable"] = stats_readable
    out["events_readable"] = events_readable

    reds = list((events or {}).get("red_cards") or [])
    goals = list((events or {}).get("goals") or [])
    explaining = ([{**r, "kind": "red_card"} for r in reds]
                  + [{**g, "kind": "goal"} for g in goals])
    # "at or before the move": the provider times events in MATCH minutes
    # while the move is bounded by two wall clocks, so the relation is an
    # ESTIMATE and is labelled one. Every explaining event at or before
    # the current elapsed minute is reported; those inside the estimated
    # window are marked, because an event 40 minutes ago explains a move
    # 40 minutes ago and this one only weakly.
    now_min = parse_number(elapsed)
    win = window_minutes if window_minutes is not None else None
    for e in explaining:
        em = parse_number(e.get("elapsed"))
        e["minutes_before_now"] = (None if (em is None or now_min is None)
                                   else round(now_min - em, 1))
        e["inside_estimated_move_window"] = (
            None if (em is None or now_min is None or win is None)
            else bool(0 <= (now_min - em) <= win))
    out["explaining_events"] = explaining
    out["explaining_event_timing_note"] = (
        "event minutes are MATCH minutes and the move window is two wall "
        "clocks, so 'inside_estimated_move_window' is an ESTIMATE that "
        "ignores stoppages and the half-time break. An event outside the "
        "window still appears here: it may explain a move only weakly, and "
        "reporting it is the honest option")

    if explaining:
        out["state"] = CONDITIONING_OBSERVED
        kinds = sorted({e["kind"] for e in explaining})
        out["reason"] = (
            f"{len(explaining)} explaining event(s) observed ({', '.join(kinds)})"
            " at or before this move")
        if (events or {}).get("any_dismissal_matched_on_unverified_string"):
            out["dismissal_string_caveat"] = (
                "at least one dismissal was matched on a detail string this "
                "repo has NOT observed from the provider "
                f"({list(RED_CARD_DETAILS_UNVERIFIED)}); the match may be "
                "spurious. It is reported rather than dropped because "
                "over-detecting an explanation weakens this finding, which "
                "is the safe direction")
    elif stats_readable and events_readable:
        out["state"] = CONDITIONING_UNOBSERVED_STATS_AVAILABLE
        out["reason"] = (
            f"live statistics readable ({stats['statistic_type_count_max']} "
            f"types) and a non-empty event feed "
            f"({events['event_count']} events) show NO goal and NO red card "
            "at or before this move")
    else:
        out["state"] = CONDITIONING_UNOBSERVED_NO_STATS
        why = []
        if not stats_readable:
            why.append(
                "no live statistics for this competition"
                if cov == COVERAGE_ABSENT else
                f"statistics not readable ({cov})")
        if not events_readable:
            why.append(
                "the event feed is EMPTY, which is 'we could not see' and "
                "never 'nothing happened' — so no explaining event can be "
                "ruled out")
        out["reason"] = "; ".join(why) or "no readable live feed"
    out["note"] = CONDITIONING_NOTE[out["state"]]
    out["strength_rank"] = CONDITIONING_STRENGTH[out["state"]]
    out["red_card_strings"] = RED_CARD_STRING_PROVENANCE
    return out


# --------------------------------------------------------------------------
# the live-xG-informed variant — ALONGSIDE the market anchor, never instead
# --------------------------------------------------------------------------
LIVE_XG_VARIANT_NOTE = (
    "SECONDARY and OPTIONAL. The PRIMARY signal this finding rests on is "
    "market-anchored and model-free: the same market's mid compared with "
    "its own previous mid, exceeding both a threshold and the wider of the "
    "two spreads. That is its whole virtue — it needs no model of ours and "
    "cannot be wrong about a model. Everything in this block is a "
    "CHARACTERISATION of the live evidence sitting beside that primary. It "
    "is NOT a probability, NOT a reprice, and NOT a second opinion that "
    "can overrule the anchor. If the two disagree, both are reported and "
    "the anchor is the one the finding is made of")


def live_xg_characterisation(reading: dict | None, join: dict | None,
                             prev_reading: dict | None = None) -> dict:
    """Territorial / xG characterisation beside the market-anchored move.

    Two readouts, because they need different evidence:

      xg_vs_score      from ONE reading: cumulative xG differential
                       against the actual goal differential. A side
                       leading while being out-created is the classic
                       "the scoreline flatters them" shape.
      xg_window_delta  needs TWO readings: xG generated between them, so
                       the move's window can be compared with what
                       happened in it. Unavailable on the first reading
                       for a fixture, and that is reported rather than
                       filled in.

    Neither is a probability. Both are labelled as characterisation.
    """
    out: dict = {"note": LIVE_XG_VARIANT_NOTE, "available": False}
    stats = (reading or {}).get("statistics")
    if not stats or not stats.get("stats_present"):
        out["reason"] = ("no live statistics for this fixture, so no "
                         "xG characterisation exists. This does not "
                         "weaken or strengthen the market-anchored "
                         "primary, which stands on its own")
        return out
    teams = stats.get("teams") or []
    if len(teams) != 2:
        out["reason"] = (f"expected 2 team entries, got {len(teams)} — "
                         "refusing to characterise a partial reading")
        return out
    if not all(t.get("xg_present") for t in teams):
        out["reason"] = (
            "xG is not present for BOTH teams (a null/'-'/non-numeric "
            "value is ABSENT, never zero), so no differential is computed. "
            "The statistic types the provider DID offer are recorded on the "
            "reading")
        out["xg_type_offered"] = stats.get("xg_type_offered")
        return out

    a, b = teams[0], teams[1]
    out["available"] = True
    out["teams"] = [{"team_id": t.get("team_id"),
                     "team_name": t.get("team_name"),
                     "xg": t.get("xg_value"),
                     "total_shots": t.get("total_shots"),
                     "shots_on_goal": t.get("shots_on_goal"),
                     "ball_possession_pct": t.get("ball_possession_pct")}
                    for t in (a, b)]
    xg_diff = round((a["xg_value"] or 0.0) - (b["xg_value"] or 0.0), 3)
    out["xg_differential_first_minus_second"] = xg_diff

    goals = (join or {}).get("goals") or {}
    gh, ga = parse_number(goals.get("home")), parse_number(goals.get("away"))
    if gh is not None and ga is not None:
        # the statistics entries are provider-ordered; orientation from the
        # join says which of them is the home side, and a reading whose
        # orientation is unknown is NOT force-fitted onto the score
        orient = (join or {}).get("orientation")
        if orient == "home_away" or orient == "away_home":
            goal_diff = gh - ga
            out["goal_differential_home_minus_away"] = goal_diff
            out["xg_vs_score"] = {
                "xg_differential": xg_diff,
                "goal_differential_home_minus_away": goal_diff,
                "reading": (
                    "the two differentials are reported side by side and "
                    "deliberately NOT combined into a single number: "
                    "mapping an xG gap onto a win probability is a model, "
                    "and this surface has no approved model for these "
                    "competitions"),
            }
    if prev_reading is not None:
        prev_stats = (prev_reading or {}).get("statistics")
        prev_teams = (prev_stats or {}).get("teams") or []
        prev_by_id = {t.get("team_id"): t for t in prev_teams
                      if t.get("xg_present")}
        deltas = []
        for t in (a, b):
            p = prev_by_id.get(t.get("team_id"))
            if p is None:
                continue
            deltas.append({"team_id": t.get("team_id"),
                           "team_name": t.get("team_name"),
                           "xg_now": t.get("xg_value"),
                           "xg_prev": p.get("xg_value"),
                           "xg_delta": round((t.get("xg_value") or 0.0)
                                             - (p.get("xg_value") or 0.0), 3)})
        if len(deltas) == 2:
            out["xg_window_delta"] = {
                "per_team": deltas,
                "prev_captured_at": prev_reading.get(
                    "statistics_captured_at"),
                "now_captured_at": (reading or {}).get(
                    "statistics_captured_at"),
                "note": ("xG accumulated BETWEEN our two readings. The two "
                         "reading clocks are ours; the window they bound is "
                         "not exactly the window the market moved in, so "
                         "this is indicative"),
            }
        else:
            out["xg_window_delta"] = {
                "available": False,
                "reason": ("the previous reading has no parseable xG for "
                           "both of these team ids, so no window delta is "
                           "computed rather than one being invented")}
    else:
        out["xg_window_delta"] = {
            "available": False,
            "reason": ("first reading for this fixture in this process — "
                       "no previous reading to difference against. Same "
                       "honest blind spot as the market pair store's first "
                       "cycle after a restart")}
    return out


# --------------------------------------------------------------------------
# the process-local previous-reading store
# --------------------------------------------------------------------------
# Process-local BY DESIGN, exactly like hunter._pair_store: persisting
# every live reading is the volume-growth pattern that filled the
# production disk on 2026-07-25 (source_observation payloads with no
# reader). Bounded in age and size.
_reading_store: dict = {}
_READING_STORE_MAX_AGE_S = 6 * 3600
_READING_STORE_MAX_ENTRIES = 2_000


def prune_reading_store(now: datetime) -> None:
    cutoff = (now - timedelta(seconds=_READING_STORE_MAX_AGE_S)).isoformat()
    for k in [k for k, v in _reading_store.items()
              if not v.get("statistics_captured_at")
              or v["statistics_captured_at"] < cutoff]:
        _reading_store.pop(k, None)
    over = len(_reading_store) - _READING_STORE_MAX_ENTRIES
    if over > 0:
        oldest = sorted(_reading_store,
                        key=lambda k: _reading_store[k].get(
                            "statistics_captured_at") or "")[:over]
        for k in oldest:
            _reading_store.pop(k, None)


def previous_reading(fixture_id: object) -> dict | None:
    return _reading_store.get(str(fixture_id))


def remember_reading(fixture_id: object, reading: dict) -> None:
    if reading and reading.get("statistics_captured_at"):
        _reading_store[str(fixture_id)] = reading


def _reset_reading_store_for_tests() -> None:
    _reading_store.clear()


# --------------------------------------------------------------------------
# coverage persistence — measured, dated, exposed
# --------------------------------------------------------------------------
def record_coverage(s, league_id: object, league_name: object,
                    league_country: object, reading: dict,
                    elapsed: object, now: datetime) -> None:
    """Upsert ONE competition's coverage verdict from a reading we actually
    took. One row per competition, updated in place — bounded growth, and
    it has a reader (`coverage_report`, and the hunter's own skip
    decision).

    A TOO_EARLY reading never overwrites a PRESENT verdict with an
    absence: that would let one early-minute read erase a measured
    presence, which is converting missing evidence into a conclusion.
    """
    from src.live.models import ApiFootballLiveCoverage
    if league_id is None:
        return
    stats = reading.get("statistics") or {}
    events = reading.get("events") or {}
    verdict = coverage_verdict(stats, elapsed)
    row = (s.query(ApiFootballLiveCoverage)
           .filter_by(league_id=int(league_id)).first())
    if row is None:
        row = ApiFootballLiveCoverage(
            league_id=int(league_id), first_measured_at=now,
            fixtures_probed=0)
        s.add(row)
    row.league_name = league_name or row.league_name
    row.league_country = league_country or row.league_country
    row.fixtures_probed = (row.fixtures_probed or 0) + 1
    row.last_measured_at = now
    row.last_elapsed_observed = (
        int(parse_number(elapsed)) if parse_number(elapsed) is not None
        else None)
    if verdict == COVERAGE_TOO_EARLY and row.verdict == COVERAGE_PRESENT:
        # keep the stronger, already-measured verdict; record that this
        # read was uninformative rather than letting it downgrade
        row.too_early_reads = (row.too_early_reads or 0) + 1
        return
    row.verdict = verdict
    row.statistic_type_count = stats.get("statistic_type_count_max") or 0
    row.statistic_types_json = json.dumps(
        stats.get("statistic_types_offered") or [], sort_keys=True)[:4000]
    row.xg_present = bool(stats.get("xg_present_both_teams"))
    row.xg_type_offered = bool(stats.get("xg_type_offered"))
    row.events_present = bool(events.get("events_present"))
    row.event_count_last = events.get("event_count")
    if verdict == COVERAGE_PRESENT:
        row.last_present_at = now


def coverage_is_fresh_absent(s, league_id: object, now: datetime) -> bool:
    """Whether a measured ABSENT verdict for this competition is still
    inside its TTL, so the cycle may skip spending 2 requests on it.

    Only ABSENT is cached this way. A PRESENT competition is always read
    fresh, because a finding's evidence must be the reading it was
    actually made from — a cached "present" would let a row assert values
    nobody looked up. And a cached absence EXPIRES, because coverage
    drifts and this repo has been broken at HTTP 200 by exactly that.
    """
    from src.live.models import ApiFootballLiveCoverage
    if league_id is None:
        return False
    row = (s.query(ApiFootballLiveCoverage)
           .filter_by(league_id=int(league_id)).first())
    if row is None or row.verdict != COVERAGE_ABSENT:
        return False
    at = row.last_measured_at
    if at is None:
        return False
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    ttl = timedelta(days=config.HUNTER_LIVE_STATS_COVERAGE_TTL_DAYS)
    return (now - at) < ttl


def coverage_report() -> dict:
    """The measured coverage table, with the date measured on every row.

    Exposed because an unexposed coverage verdict is a private assumption,
    and this module's whole claim is that coverage is measured rather than
    assumed.
    """
    from src.live.db import get_session, plane_ready
    from src.live.models import ApiFootballLiveCoverage
    if not plane_ready():
        return {"ready": False, "reason": "live plane dormant"}
    s = get_session()
    try:
        rows = (s.query(ApiFootballLiveCoverage)
                .order_by(ApiFootballLiveCoverage.league_id).all())
        return {
            "ready": True,
            "enabled": config.HUNTER_LIVE_STATS_ENABLED,
            "key_configured": bool(load_key()[0]),
            "in_play_alerts_enabled": config.HUNTER_IN_PLAY_ALERTS_ENABLED,
            "budget": day_spend(),
            "competition_map": COMPETITION_MAP,
            "competition_map_note": COMPETITION_MAP_NOTE,
            "competition_map_fingerprint": competition_map_fingerprint(),
            "conditioning_states": {
                st: {"note": CONDITIONING_NOTE[st],
                     "strength_rank": CONDITIONING_STRENGTH[st]}
                for st in CONDITIONING_STATES},
            "inference_direction": (
                "strength_rank orders how much each state supports reading "
                "a move as an OVERREACTION. conditioning_observed ranks "
                "LOWEST: an explaining event makes the market's move more "
                "reasonable, so finding a reason WEAKENS the finding"),
            "red_card_strings": RED_CARD_STRING_PROVENANCE,
            "measurement_rule": {
                "stats_present": ("len(response) > 0 and at least one "
                                  "statistic with a non-empty type"),
                "xg_present": ("parseable number; null/''/'-'/non-numeric "
                               "is ABSENT, never zero"),
                "events_present": "len(response) > 0",
                "too_early": (f"absence below {MIN_HONEST_ELAPSED}' is "
                              "TOO_EARLY_TO_CONCLUDE, never ABSENT"),
                "separate_verdicts": ("statistics and events coverage are "
                                      "tracked separately because events "
                                      "coverage is strictly BROADER — "
                                      "measured: competitions serving goal "
                                      "events with zero statistic types"),
            },
            "coverage_ttl_days":
                config.HUNTER_LIVE_STATS_COVERAGE_TTL_DAYS,
            "coverage_ttl_note": (
                "only an ABSENT verdict is cached to save requests, and it "
                "EXPIRES; a PRESENT competition is always read fresh so a "
                "finding's evidence is the reading it was made from"),
            "competitions": [{
                "league_id": r.league_id,
                "league_name": r.league_name,
                "league_country": r.league_country,
                "verdict": r.verdict,
                "statistic_type_count": r.statistic_type_count,
                "statistic_types": (json.loads(r.statistic_types_json)
                                    if r.statistic_types_json else None),
                "xg_present": r.xg_present,
                "xg_type_offered": r.xg_type_offered,
                "events_present": r.events_present,
                "event_count_last": r.event_count_last,
                "fixtures_probed": r.fixtures_probed,
                "too_early_reads": r.too_early_reads,
                "last_elapsed_observed": r.last_elapsed_observed,
                "first_measured_at": (r.first_measured_at.isoformat()
                                      if r.first_measured_at else None),
                "last_measured_at": (r.last_measured_at.isoformat()
                                     if r.last_measured_at else None),
                "last_present_at": (r.last_present_at.isoformat()
                                    if r.last_present_at else None),
            } for r in rows],
            "denominator_note": (
                "fixtures_probed is the denominator: a verdict from one "
                "fixture at 20' and a verdict from ten at 70' are not the "
                "same evidence and must not read the same"),
        }
    finally:
        s.close()
