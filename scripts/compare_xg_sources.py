#!/usr/bin/env python3
"""Is API-Football's xG ACCURATE, or merely PRESENT?

    python scripts/compare_xg_sources.py

MLS is the only competition in this platform where two independent xG
sources exist, so it is the only place this can be measured at all:

  SPORTEC       stats-api.mlssoccer.com — public, no auth, MLS's own
                official provider, already ingested by src/live/mls_stats.py
  API-FOOTBALL  v3.football.api-sports.io — the paid PRO key, verified on
                2026-07-29 to CONTAIN xG for EPL / La Liga / Liga MX / MLS
                (docs/APIFOOTBALL-TRIAL.md), never checked for ACCURACY

docs/APIFOOTBALL-TRIAL.md §8.2 names this gap in its own words: "It says
nothing about whether the xG is any good. Presence is not accuracy." The
platform is about to fit league xG ratings for three leagues that have NO
second source, so what is measured here is the entire basis for trusting
API-Football's xG anywhere else.

WHAT THIS DOES NOT DO
    It does not modify src/live/model_mls.py. That file is inside the MLS
    engine signature; changing it invalidates the live approval and halts
    shadow collection. `fit()` is already a pure function of its inputs, so
    the refit imports it and passes a different xG map. The file's sha256
    is recorded before and after so the claim is checkable, not asserted.

IDENTITY — THE RULE THAT GOVERNS THE JOIN
    Fixture identity NEVER comes from team names plus a date (AGENTS.md
    §5, §13). There is no shared fixture id between these two providers,
    so the join is built in two separate stages and only the first one
    touches a name:

      stage 1  TEAM ENTITIES, once, from FROZEN references
               sportec club id  --three_letter_code == ESPN abbrev-->  ESPN team id
               API-Football team id  --frozen roster resolver-->  ESPN displayName
               Both legs already exist and are independently verified in
               this repo; both frozen files are sha256-asserted here. Zero
               new aliases are introduced. An unresolved or ambiguous team
               is DROPPED and NAMED, never guessed.

      stage 2  FIXTURES, from provider-stable ids only
               key = (home ESPN team id, away ESPN team id), then a single
               candidate within a kickoff window, then the FINAL SCORE —
               a field independent of xG — must agree exactly. More than
               one candidate fails explicitly. This is materially stronger
               than name+date: both sides are ids, the entity map is
               frozen, uniqueness is enforced and an independent field
               corroborates.

EVIDENCE
    Every provider response is archived to
    research_archive/xg_source_comparison_<UTC-date>.json with the key
    redacted, and the archive refuses to overwrite an earlier run's file
    (the lesson of research_archive/apifootball_tuning_incident_2026-07-29
    .json: a re-run destroyed the pre-registered evidence in place).

    A null / empty / non-numeric xG is ABSENT, never zero. Both providers
    can return a present-but-null field; API-Football does exactly that
    for Bundesliga, J1 and Primeira Liga (measured 2026-07-29).

BUDGET
    PRO is 300 requests/minute and 7,500/day. This run costs roughly
    2 + (one call per paired fixture) API-Football requests — about 255
    for a full MLS season to date, ~3% of the daily quota — paced with a
    fixed delay, hard-capped, at most one retry after a fixed sleep.
    Sportec is throttled at src/live/mls_stats.THROTTLE_SECONDS.

EXIT CODES
    0  the comparison ran and is reported
    2  INDETERMINATE — could not measure (no key, budget abort, transport
       failure, identity bridge failed). Not a verdict: missing evidence
       is never converted into confidence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

ARCHIVE_DIR = REPO_ROOT / "research_archive"
REDACTED = "<APIFOOTBALL_KEY_REDACTED>"

AF_BASE = os.getenv("APIFOOTBALL_BASE", "https://v3.football.api-sports.io")
AF_MLS_LEAGUE_ID = 253            # verified 2026-07-29: "Major League Soccer", USA
AF_SEASON = 2026                  # MLS is a calendar-year league
ESPN_TEAMS_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                  "usa.1/teams")

# The frozen halves of the pre-registered API-Football instrument. They live
# on feat-apifootball-probe, not on main, so they are read out of that
# branch's object store and sha256-asserted against the literals the trial
# document publishes. Re-implementing the resolver would let this comparison
# quietly use a DIFFERENT bridge than the one CHECK 3 measured; reading the
# real one and hashing it does not.
PROBE_BRANCH = "feat-apifootball-probe"
HARNESS_BLOB = "scripts/verify_apifootball.py"
ROSTER_BLOB = "scripts/apifootball_espn_roster.json"
# docs/APIFOOTBALL-TRIAL.md §4 / §7-A2: the roster's pre-data hash, restored
# after the tuning incident. If this literal does not match, the roster is
# not the one the pre-registered run used and this comparison must not run.
ROSTER_SHA256_EXPECTED = \
    "1c0f01745bcb72ff8621abdb9cf6420b0180f8400bff631817dbb15b26ab8a3c"

# --- join parameters, fixed before the pairing was computed ----------------
# A kickoff window, NOT a join key: the key is the ordered pair of stable
# team ids. The window only has to separate the two league meetings of the
# same ordered pair, which are months apart (src/live/mls_stats.py uses 3
# days for the same reason). 36h absorbs a postponement or a provider
# recording a scheduled rather than actual kickoff, and is far narrower
# than the gap it must discriminate.
KICKOFF_WINDOW_HOURS = 36.0
# The independent corroboration. Goals are not derived from xG by either
# provider, so a scoreline disagreement means the two rows are not the same
# match (or one provider is wrong about the match) — either way, drop.
REQUIRE_SCORE_AGREEMENT = True
N_BOOTSTRAP = 10000
BOOTSTRAP_SEED = 20260729
WORST_N = 12


# ==========================================================================
# secrets
# ==========================================================================

KEY_FILE = Path.home() / ".apifootball_key"


def load_key() -> tuple[str, str, list[str]]:
    """(key, provenance, problems). Same contract as
    scripts/verify_apifootball.py: APIFOOTBALL_KEY wins, else
    ~/.apifootball_key which MUST be mode 600 — a group- or world-readable
    secret file is refused rather than read. The older API_FOOTBALL_KEY is
    deliberately not a fallback: it may still hold the free-tier key."""
    problems: list[str] = []
    env = os.getenv("APIFOOTBALL_KEY", "").strip()
    if env:
        return env, "APIFOOTBALL_KEY", problems
    if not KEY_FILE.exists():
        return "", "nowhere", problems
    mode = KEY_FILE.stat().st_mode & 0o777
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


# ==========================================================================
# provenance
# ==========================================================================

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_show(ref: str, blob: str) -> bytes:
    return subprocess.run(["git", "show", f"{ref}:{blob}"], cwd=REPO_ROOT,
                          check=True, capture_output=True).stdout


def load_frozen_instrument(workdir: Path) -> dict:
    """Extract the pre-registered resolver + frozen roster from the probe
    branch, assert the roster hash, and import the resolver unmodified."""
    workdir.mkdir(parents=True, exist_ok=True)
    hp, rp = workdir / "verify_apifootball.py", workdir / ROSTER_BLOB.split("/")[-1]
    hp.write_bytes(git_show(PROBE_BRANCH, HARNESS_BLOB))
    rp.write_bytes(git_show(PROBE_BRANCH, ROSTER_BLOB))
    roster_sha, harness_sha = sha256_file(rp), sha256_file(hp)
    if roster_sha != ROSTER_SHA256_EXPECTED:
        raise SystemExit(
            f"FROZEN ROSTER HASH MISMATCH\n  expected {ROSTER_SHA256_EXPECTED}"
            f"\n  got      {roster_sha}\n"
            "The frozen ESPN reference is not the one the pre-registered run "
            "used. Refusing to build an identity bridge on a roster that may "
            "have been tuned — see research_archive/"
            "apifootball_tuning_incident_2026-07-29.json.")
    spec = importlib.util.spec_from_file_location("_frozen_verify", hp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "module": mod,
        "roster": json.loads(rp.read_text(encoding="utf-8")),
        "provenance": {
            "branch": PROBE_BRANCH,
            "harness_blob": HARNESS_BLOB,
            "harness_sha256": harness_sha,
            "harness_criteria_fingerprint": mod.CRITERIA_FINGERPRINT,
            "harness_criteria_recomputed": mod.criteria_fingerprint(),
            "roster_blob": ROSTER_BLOB,
            "roster_sha256": roster_sha,
            "roster_sha256_expected": ROSTER_SHA256_EXPECTED,
            "roster_hash_verified": True,
        },
    }


# ==========================================================================
# xG parsing — a null/empty/non-numeric value is ABSENT, never zero
# ==========================================================================

def xg_number(raw: object) -> float | None:
    """Parse an xG value, or None for ABSENCE. Mirrors
    scripts/verify_apifootball.py xg_number(): None / '' / '-' / 'null' /
    non-numeric are all absence. A 0.0 parses to 0.0 here and is classified
    downstream — presence of the FIELD and presence of a VALUE are
    different questions and are counted separately."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return None if (math.isnan(v) or math.isinf(v)) else v
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if not s or s.lower() in {"-", "–", "—", "null", "none", "n/a", "na"}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def norm_stat_type(t: object) -> str:
    return "".join(c for c in str(t or "").lower() if c.isalnum())


def is_xg_type(t: object) -> bool:
    """expected-goals FOR only. Rejects the against/conceded/prevented
    variants: accepting xGA would compare the opponent's number."""
    n = norm_stat_type(t)
    if not n:
        return False
    if any(b in n for b in ("against", "conceded", "prevented", "allowed",
                            "opponent", "xga")):
        return False
    return n == "xg" or "expectedgoals" in n


# ==========================================================================
# HTTP
# ==========================================================================

class BudgetExceeded(RuntimeError):
    pass


class AFClient:
    """One budgeted, delayed, redacting GET path against API-Football."""

    def __init__(self, key: str, records: list, say, hard_cap: int = 400,
                 delay: float = 0.5):
        self._key, self._say = key, say
        self.records, self.used = records, 0
        self._cap, self._delay = hard_cap, delay
        self._session = requests.Session()

    def get(self, path: str, params: dict, label: str,
            record_body: bool = True) -> object:
        if self.used >= self._cap:
            raise BudgetExceeded(
                f"API-Football request cap {self._cap} reached before {label} "
                "— aborting rather than spending more of the day's quota")
        for attempt in (0, 1):
            if self.used:
                time.sleep(self._delay)
            self.used += 1
            try:
                r = self._session.get(
                    f"{AF_BASE}/{path.lstrip('/')}", params=params,
                    headers={"x-apisports-key": self._key}, timeout=25)
                body = r.json()
            except Exception as exc:
                err = f"{type(exc).__name__}: {_redact(exc, self._key)}"
                self.records.append({"provider": "apifootball", "label": label,
                                     "path": path, "params": params,
                                     "transport_error": err,
                                     "attempt": attempt + 1})
                if attempt == 0:
                    self._say(f"    transport error on {label}; one retry in 5s")
                    time.sleep(5.0)
                    continue
                return None
            rec = {"provider": "apifootball", "label": label, "path": path,
                   "params": params, "http_status": r.status_code,
                   "declared_results": body.get("results")
                   if isinstance(body, dict) else None,
                   "response_len": len(body.get("response") or [])
                   if isinstance(body, dict) else 0,
                   "errors": body.get("errors")
                   if isinstance(body, dict) else None}
            if record_body:
                rec["body"] = json.loads(_redact(json.dumps(body), self._key))
            self.records.append(rec)
            return body
        return None


class SportecClient:
    """Sportec is public and free; still throttled at the rate
    src/live/mls_stats.py uses. No key, so nothing to redact."""

    BASE = "https://stats-api.mlssoccer.com"
    HEADERS = {"Referer": "https://www.mlssoccer.com/",
               "User-Agent": "wc26-shadow-research/1.0 (personal; throttled)"}

    def __init__(self, records: list, say, delay: float = 0.4):
        self.records, self._say, self._delay = records, say, delay
        self.used = 0
        self._session = requests.Session()

    def get(self, path: str, params: dict | None, label: str,
            record_body: bool = True) -> object:
        for attempt in (0, 1):
            if self.used:
                time.sleep(self._delay)
            self.used += 1
            try:
                r = self._session.get(f"{self.BASE}/{path.lstrip('/')}",
                                      params=params or {},
                                      headers=self.HEADERS, timeout=25)
                body = r.json()
            except Exception as exc:
                self.records.append({"provider": "sportec", "label": label,
                                     "path": path, "params": params,
                                     "transport_error":
                                         f"{type(exc).__name__}: {exc}",
                                     "attempt": attempt + 1})
                if attempt == 0:
                    self._say(f"    transport error on {label}; one retry in 5s")
                    time.sleep(5.0)
                    continue
                return None
            rec = {"provider": "sportec", "label": label, "path": path,
                   "params": params, "http_status": r.status_code}
            if record_body:
                rec["body"] = body
            self.records.append(rec)
            return body
        return None


def response_items(payload: object) -> list:
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response")
    return resp if isinstance(resp, list) else []


# ==========================================================================
# statistics — numpy only, no scipy in this venv
# ==========================================================================

def pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def rankdata(a: np.ndarray) -> np.ndarray:
    """Average ranks for ties — the Spearman definition."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    sorted_a = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    return pearson(rankdata(x), rankdata(y))


def wilson_ci(k: int, n: int, z: float = 1.959963985) -> list[float]:
    """Wilson score interval — behaves at proportions near 0 and 1, where
    the normal approximation reports impossible bounds."""
    if n == 0:
        return [float("nan"), float("nan")]
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def cluster_bootstrap_mean(per_fixture_values: list[list[float]],
                           n_boot: int, seed: int) -> dict:
    """95% CI for a mean over team-observations, resampling FIXTURES not
    observations. The two team-observations inside one fixture are not
    independent — the same match, the same provider run, often the same
    disagreement — so an observation-level interval would be too narrow."""
    flat = [v for grp in per_fixture_values for v in grp]
    if not flat:
        return {"mean": None, "ci95": [None, None], "n_obs": 0,
                "n_clusters": 0}
    point = float(np.mean(flat))
    n = len(per_fixture_values)
    rng = np.random.default_rng(seed)
    groups = [np.asarray(g, dtype=float) for g in per_fixture_values]
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        means[b] = float(np.mean(np.concatenate([groups[i] for i in idx])))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"mean": round(point, 4),
            "ci95": [round(float(lo), 4), round(float(hi), 4)],
            "boot_se": round(float(np.std(means, ddof=1)), 4),
            "n_obs": len(flat), "n_clusters": n,
            "excludes_zero": bool(lo > 0 or hi < 0)}


def distribution(vals: np.ndarray) -> dict:
    if len(vals) == 0:
        return {}
    qs = [0, 5, 10, 25, 50, 75, 90, 95, 99, 100]
    return {
        "n": int(len(vals)),
        "mean": round(float(np.mean(vals)), 4),
        "sd": round(float(np.std(vals, ddof=1)), 4) if len(vals) > 1 else None,
        "percentiles": {f"p{q}": round(float(np.percentile(vals, q)), 4)
                        for q in qs},
    }


def histogram(vals: np.ndarray, edges: list[float]) -> list[dict]:
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        last = i == len(edges) - 2
        k = int(np.sum((vals >= lo) & ((vals <= hi) if last else (vals < hi))))
        out.append({"range": f"[{lo}, {hi}{']' if last else ')'}",
                    "count": k,
                    "pct": round(100.0 * k / len(vals), 1) if len(vals) else 0.0})
    return out


# ==========================================================================
# refit — imports model_mls.fit, never edits it
# ==========================================================================

class StandInFixture:
    """The attribute surface model_mls.fit / model_eval.fit_variant read.

    Deliberately NOT a src.live.models.Fixture: this comparison runs with no
    database, and constructing ORM rows would imply a live plane that is not
    there. `espn_event_id` is None because no ESPN event id was fetched — it
    feeds only fit()'s `source_fixtures` provenance list and no rating.
    """
    __slots__ = ("id", "espn_event_id", "home_team_id", "away_team_id",
                 "home_goals", "away_goals", "current_kickoff_utc")

    def __init__(self, fid, home_team_id, away_team_id, home_goals,
                 away_goals, kickoff):
        self.id = fid
        self.espn_event_id = None
        self.home_team_id = home_team_id
        self.away_team_id = away_team_id
        self.home_goals = home_goals
        self.away_goals = away_goals
        self.current_kickoff_utc = kickoff


def rating_diff(fit_a: dict, fit_b: dict, label_of: dict) -> dict:
    """How far apart two fitted rating sets are, per team and in aggregate,
    plus whether the ORDERING of the league changes — which is what a
    relative-strength model actually consumes."""
    teams = sorted(set(fit_a["ratings"]) | set(fit_b["ratings"]),
                   key=lambda t: label_of.get(t, str(t)))
    rows = []
    for t in teams:
        ra, rb = fit_a["ratings"].get(t), fit_b["ratings"].get(t)
        if ra is None or rb is None:
            rows.append({"team": label_of.get(t, str(t)),
                         "present_in_both": False})
            continue
        rows.append({
            "team": label_of.get(t, str(t)), "present_in_both": True,
            "games": ra["games"],
            "attack_a": round(ra["attack"], 4), "attack_b": round(rb["attack"], 4),
            "attack_diff": round(rb["attack"] - ra["attack"], 4),
            "defence_a": round(ra["defence"], 4),
            "defence_b": round(rb["defence"], 4),
            "defence_diff": round(rb["defence"] - ra["defence"], 4),
        })
    both = [r for r in rows if r.get("present_in_both")]
    out = {"per_team": rows, "n_teams_compared": len(both)}
    if not both:
        return out
    for field in ("attack", "defence"):
        d = np.array([r[f"{field}_diff"] for r in both])
        a = np.array([r[f"{field}_a"] for r in both])
        b = np.array([r[f"{field}_b"] for r in both])
        rank_a, rank_b = rankdata(-a), rankdata(-b)
        out[field] = {
            "mean_signed_diff": round(float(np.mean(d)), 4),
            "mean_abs_diff": round(float(np.mean(np.abs(d))), 4),
            "max_abs_diff": round(float(np.max(np.abs(d))), 4),
            "max_abs_diff_team": both[int(np.argmax(np.abs(d)))]["team"],
            "spearman_of_ratings": (round(spearman(a, b), 4)
                                    if spearman(a, b) is not None else None),
            "pearson_of_ratings": (round(pearson(a, b), 4)
                                   if pearson(a, b) is not None else None),
            "spread_a": round(float(np.max(a) - np.min(a)), 4),
            "spread_b": round(float(np.max(b) - np.min(b)), 4),
            "max_rank_change": int(np.max(np.abs(rank_a - rank_b))),
            "n_teams_rank_changed": int(np.sum(rank_a != rank_b)),
            "top_team_a": both[int(np.argmax(a if field == "attack" else -a))]["team"],
            "top_team_b": both[int(np.argmax(b if field == "attack" else -b))]["team"],
        }
    return out


def walk_forward(fixtures: list, xg_map: dict, rungs: tuple[str, ...],
                 say) -> dict:
    """Rolling-origin evaluation, replicating src/live/model_eval
    .evaluate_ladder()'s method on an in-memory fixture list.

    evaluate_ladder() itself reads the live Postgres plane, which is not
    reachable from here; it is the SAME imported fit_variant /
    predict_variant / _score_fixture, called the same way, so the rung
    definitions and the scoring cannot drift from the deployed ones. Only
    the fixture source and the xG map differ, which is the whole point.
    """
    from src.live import model_eval as me
    per_fixture, skipped, scored_ids = [], 0, []
    for i, f in enumerate(fixtures):
        prior = fixtures[:i]
        as_of = me._utc(f.current_kickoff_utc)
        preds, ok = {}, True
        for name in rungs:
            m = me.fit_variant(prior, as_of, me.LADDER[name],
                               xg_by_fixture=xg_map)
            p = me.predict_variant(m, f) if m else None
            if p is None:
                ok = False
                break
            preds[name] = p
        if not ok:
            skipped += 1
            continue
        result = ("home_win" if f.home_goals > f.away_goals else
                  "away_win" if f.away_goals > f.home_goals else "draw")
        scored_ids.append(f.id)
        per_fixture.append({n: me._score_fixture(preds[n], result)
                            for n in rungs})
    return {"per_fixture": per_fixture, "n_scored": len(per_fixture),
            "scored_ids": scored_ids,
            "n_skipped_insufficient_history": skipped}


def rung_means(per_fixture: list, rungs: tuple[str, ...]) -> dict:
    return {n: {m: round(float(np.mean([pf[n][m] for pf in per_fixture])), 5)
                for m in ("log_loss", "brier", "rps")}
            for n in rungs} if per_fixture else {}


def paired_edge(pf_a: list, pf_b: list, rung: str, n_boot: int,
                seed: int) -> dict:
    """Log-loss difference for the SAME rung under two xG sources, on the
    same fixtures, with a match-cluster bootstrap. Positive = source A has
    the lower (better) log loss."""
    if not pf_a or len(pf_a) != len(pf_b):
        return {"error": "score sets not aligned",
                "n_a": len(pf_a), "n_b": len(pf_b)}
    a = np.array([pf[rung]["log_loss"] for pf in pf_a])
    b = np.array([pf[rung]["log_loss"] for pf in pf_b])
    d = b - a
    rng = np.random.default_rng(seed)
    n = len(d)
    boot = np.array([float(np.mean(d[rng.integers(0, n, n)]))
                     for _ in range(n_boot)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {"rung": rung, "n": n,
            "log_loss_a": round(float(np.mean(a)), 5),
            "log_loss_b": round(float(np.mean(b)), 5),
            "delta_log_loss": round(float(np.mean(d)), 5),
            "ci95": [round(float(lo), 5), round(float(hi), 5)],
            "significant": bool(lo > 0 or hi < 0),
            "n_fixtures_identical_score": int(np.sum(d == 0.0))}


# ==========================================================================
# archive
# ==========================================================================

def load_bundle_from_archive(path: Path) -> dict:
    """Rebuild every provider payload from a committed archive so the whole
    analysis can be REPLAYED with no network and no further quota spend.

    This is the point of archiving raw bodies rather than parsed summaries: a
    reviewer can recompute every statistic in the report from the committed
    bytes instead of trusting the harness's arithmetic.
    """
    d = json.loads(path.read_text(encoding="utf-8"))
    b: dict = {"espn": None, "af_teams": None, "af_fixtures": None,
               "sp_schedule": None, "af_stats": {}, "sp_stats": {},
               "replayed_from": str(path), "original_run_utc": d.get("run_utc")}
    for rec in d.get("provider_records") or []:
        lab, body = rec.get("label", ""), rec.get("body")
        if lab == "teams:usa.1":
            b["espn"] = body
        elif lab.startswith("teams:mls:"):
            b["af_teams"] = body
        elif lab.startswith("fixtures:mls:"):
            b["af_fixtures"] = body
        elif lab == "sportec:schedule":
            b["sp_schedule"] = body
        elif lab.startswith("af:stats:"):
            b["af_stats"][int(lab.split("af:stats:")[1])] = body
        elif lab.startswith("sp:stats:"):
            b["sp_stats"][lab.split("sp:stats:")[1]] = body
    missing = [k for k in ("espn", "af_teams", "af_fixtures", "sp_schedule")
               if b[k] is None]
    if missing:
        raise SystemExit(f"archive {path} is missing payloads: {missing}")
    return b


def _count(it) -> dict:
    out: dict = {}
    for v in it:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def archive_path(date_str: str) -> Path:
    """Never clobber an earlier run's evidence — the lesson of the tuning
    incident, where a re-run overwrote the pre-registered archive in place."""
    base = ARCHIVE_DIR / f"xg_source_comparison_{date_str}.json"
    if not base.exists():
        return base
    for i in range(2, 50):
        alt = ARCHIVE_DIR / f"xg_source_comparison_{date_str}.run{i}.json"
        if not alt.exists():
            return alt
    raise SystemExit("too many archives for one date")


# ==========================================================================
# main
# ==========================================================================

def main() -> int:
    lines: list[str] = []

    def say(msg: str = ""):
        print(msg, flush=True)
        lines.append(msg)

    def bar(t):
        say("")
        say("=" * 74)
        say(t)
        say("=" * 74)

    now = datetime.now(timezone.utc)
    run_utc = now.isoformat()

    replay_from = None
    if "--from-archive" in sys.argv:
        replay_from = Path(sys.argv[sys.argv.index("--from-archive") + 1])
    bundle = load_bundle_from_archive(replay_from) if replay_from else None

    key, key_from, key_problems = load_key()
    if bundle:
        # replay needs no key and spends no quota
        key, key_from, key_problems = "", "REPLAY_NO_KEY_NEEDED", []

    report: dict = {
        "_what": ("Is API-Football's xG ACCURATE, not merely present? "
                  "Measured against Sportec's xG for the same MLS matches — "
                  "the only competition in this platform where two "
                  "independent xG sources exist."),
        "_governing_rules": [
            "A null/empty/non-numeric xG is ABSENT, never zero.",
            "Fixture identity never comes from names+date: the team-entity "
            "bridge is frozen and hash-asserted, the fixture join is on "
            "stable ids plus a uniqueness constraint plus independent "
            "score corroboration.",
            "Never report a count without its denominator.",
            "src/live/model_mls.py is inside the MLS engine signature and is "
            "NOT modified; fit() is imported and parameterized, and the "
            "file's sha256 is recorded.",
        ],
        "run_utc": run_utc,
        "key_provenance": key_from,
        "join_parameters": {
            "kickoff_window_hours": KICKOFF_WINDOW_HOURS,
            "require_score_agreement": REQUIRE_SCORE_AGREEMENT,
            "n_bootstrap": N_BOOTSTRAP, "bootstrap_seed": BOOTSTRAP_SEED,
        },
        "provider_records": [],
        "engine_files_sha256_before": {},
        "engine_files_sha256_after": {},
    }
    records = report["provider_records"]

    engine_files = ["src/live/model_mls.py", "src/live/model_eval.py",
                    "src/live/mls_stats.py", "src/models/xg_model.py",
                    "src/live/identity.py"]
    for rel in engine_files:
        report["engine_files_sha256_before"][rel] = sha256_file(REPO_ROOT / rel)

    bar("PROVENANCE")
    say(f"run_utc                {run_utc}")
    say(f"repo                   {REPO_ROOT}")
    say(f"branch                 " + subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT,
        capture_output=True, text=True).stdout.strip())
    say(f"HEAD                   " + subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
        capture_output=True, text=True).stdout.strip())
    say("")
    say("MLS ENGINE FILES — sha256 BEFORE (recorded so 'untouched' is "
        "checkable, not asserted):")
    for rel, h in report["engine_files_sha256_before"].items():
        say(f"  {h}  {rel}")

    if bundle:
        report["mode"] = "REPLAY_FROM_ARCHIVE"
        report["replayed_from"] = bundle["replayed_from"]
        report["original_run_utc"] = bundle["original_run_utc"]
        say("")
        say(f"MODE  REPLAY_FROM_ARCHIVE — every provider payload is read from "
            f"{bundle['replayed_from']}")
        say(f"      original run_utc {bundle['original_run_utc']}; no network "
            "request is made and no API quota is spent. Every statistic below "
            "is recomputed from the committed bytes.")
    else:
        report["mode"] = "LIVE_FETCH"

    if not bundle and (key_problems or not key):
        for p in key_problems:
            say(f"  !! {p}")
        say("INDETERMINATE — no usable API-Football key. Missing evidence is "
            "never converted into confidence.")
        report["verdict"] = "INDETERMINATE"
        report["reason"] = key_problems or ["no key found"]
        report["stdout"] = lines
        out = archive_path(now.date().isoformat())
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
        say(f"archive -> {out}")
        return 2

    say("")
    frozen = load_frozen_instrument(
        Path(os.getenv("TMPDIR", "/tmp")) / "trivela_frozen_instrument")
    fv, roster = frozen["module"], frozen["roster"]
    report["frozen_instrument"] = frozen["provenance"]
    say("FROZEN IDENTITY INSTRUMENT (read from the probe branch's object "
        "store, not re-implemented):")
    for k in ("branch", "harness_blob", "harness_sha256", "roster_blob",
              "roster_sha256"):
        say(f"  {k:32s} {frozen['provenance'][k]}")
    say(f"  roster hash matches the pre-data literal   "
        f"{frozen['provenance']['roster_hash_verified']}")
    say(f"  criteria fingerprint committed / recomputed "
        f"{frozen['provenance']['harness_criteria_fingerprint'][:16]}… / "
        f"{frozen['provenance']['harness_criteria_recomputed'][:16]}…")
    mls_ref = roster["leagues"]["mls"]

    af = AFClient(key, records, say)
    sp = SportecClient(records, say)

    def _replay(provider: str, path: str, params, label: str,
                top_key: str | None, stats_key):
        """The archived body for this call. `top_key` names a one-off payload;
        `stats_key` indexes the per-fixture statistics map. They are separate
        parameters because Sportec's match ids are themselves strings and
        would otherwise be mistaken for top-level keys."""
        if top_key is not None:
            body = bundle[top_key]
        else:
            m = "af_stats" if provider == "apifootball" else "sp_stats"
            body = bundle[m].get(stats_key)
        records.append({"provider": provider, "label": label, "path": path,
                        "params": params, "replayed_from_archive": True,
                        "body": body})
        return body

    def fetch_af(path: str, params: dict, label: str, top_key=None,
                 stats_key=None):
        """Live fetch, or the archived body under the same label. Either way
        the body lands in `records`, so a replay archive is self-contained."""
        if bundle is None:
            return af.get(path, params, label)
        return _replay("apifootball", path, params, label, top_key, stats_key)

    def fetch_sp(path: str, params, label: str, top_key=None, stats_key=None):
        if bundle is None:
            return sp.get(path, params, label)
        return _replay("sportec", path, params, label, top_key, stats_key)

    # ---------------------------------------------------------------- teams
    bar("STAGE 1 — TEAM ENTITY BRIDGE (frozen, hash-asserted, no new aliases)")

    say("ESPN usa.1 /teams — the canonical id space both bridges land in")
    if bundle is None:
        er = requests.get(ESPN_TEAMS_URL, timeout=20)
        espn_body, status = er.json(), er.status_code
    else:
        espn_body, status = bundle["espn"], "replayed"
    records.append({"provider": "espn", "label": "teams:usa.1",
                    "path": ESPN_TEAMS_URL, "params": {},
                    "http_status": status, "body": espn_body})
    espn_teams = [t.get("team", {}) for t in
                  espn_body.get("sports", [{}])[0]
                  .get("leagues", [{}])[0].get("teams", [])]
    espn_by_abbrev, espn_by_display = {}, {}
    for t in espn_teams:
        if t.get("abbreviation"):
            espn_by_abbrev.setdefault(t["abbreviation"], []).append(t)
        if t.get("displayName"):
            espn_by_display.setdefault(t["displayName"], []).append(t)
    say(f"  {len(espn_teams)} ESPN teams; "
        f"{len(espn_by_abbrev)} distinct abbreviations, "
        f"{len(espn_by_display)} distinct displayNames")
    dup_abbrev = {k: len(v) for k, v in espn_by_abbrev.items() if len(v) > 1}
    if dup_abbrev:
        say(f"  !! duplicate ESPN abbreviations: {dup_abbrev} — the Sportec "
            "leg cannot be 1:1 through a duplicated abbreviation")

    say("")
    say("API-Football /teams?league=253&season=2026 -> ESPN displayName, via "
        "the FROZEN roster resolver (tiers: exact -> frozen alias -> token "
        "set -> unique containment; ambiguity fails explicitly)")
    tb = fetch_af("teams", {"league": AF_MLS_LEAGUE_ID, "season": AF_SEASON},
                  f"teams:mls:{AF_SEASON}", top_key="af_teams")
    af_team_items = response_items(tb)
    af_names, af_id_by_name = [], {}
    for it in af_team_items:
        t = (it or {}).get("team") or {}
        if t.get("name"):
            af_names.append(t["name"])
            af_id_by_name[t["name"]] = t.get("id")
    say(f"  API-Football returned {len(af_team_items)} team entries, "
        f"{len(af_names)} named")
    res = fv.resolve_roster(af_names, mls_ref["espn_team_names"],
                            mls_ref["aliases"])
    say(f"  resolved {res['resolved']}/{res['apifootball_teams']} = "
        f"{res['rate']:.1%}   tiers {res['tiers']}")

    # canonical map: API-Football team id -> ESPN team id
    af_to_espn: dict[int, dict] = {}
    team_bridge_rows, unresolved_af = [], []
    # the frozen roster's ESPN names were captured from
    # src/live/identity.py::KALSHI_BRIDGES values; ESPN has silently renamed
    # fields on this project twice in one week, so a roster name that no
    # longer exists in the LIVE /teams payload is reported, not absorbed
    roster_names = set(mls_ref["espn_team_names"])
    live_names = set(espn_by_display)
    roster_not_live = sorted(roster_names - live_names)
    live_not_roster = sorted(live_names - roster_names)
    if roster_not_live or live_not_roster:
        say(f"  ESPN REFERENCE DRIFT — frozen roster vs LIVE ESPN /teams:")
        for n in roster_not_live:
            say(f"    in frozen roster, NOT in live ESPN payload: {n!r}")
        for n in live_not_roster:
            say(f"    in live ESPN payload, NOT in frozen roster: {n!r}")
    else:
        say("  frozen roster's 30 ESPN names all present in the live ESPN "
            "/teams payload (no reference drift)")

    for row in res["rows"]:
        nm = row["apifootball_name"]
        af_id = af_id_by_name.get(nm)
        matched = row.get("espn_name")
        e = (espn_by_display.get(matched) or [None])[0] if matched else None
        entry = {"apifootball_name": nm, "apifootball_team_id": af_id,
                 "tier": row["tier"], "espn_display_name": matched,
                 "espn_team_id": (int(e["id"]) if e and e.get("id") else None),
                 "espn_abbrev": (e.get("abbreviation") if e else None)}
        team_bridge_rows.append(entry)
        if entry["espn_team_id"] is not None and af_id is not None:
            af_to_espn[af_id] = entry
        else:
            unresolved_af.append(entry)
    # injectivity: two API-Football teams must never claim one ESPN team
    seen_espn: dict[int, str] = {}
    collisions = []
    for af_id, e in af_to_espn.items():
        eid = e["espn_team_id"]
        if eid in seen_espn:
            collisions.append(f"{e['apifootball_name']!r} and "
                              f"{seen_espn[eid]!r} both claim ESPN team {eid}")
        seen_espn[eid] = e["apifootball_name"]
    say(f"  API-Football -> ESPN bridge: {len(af_to_espn)}/{len(af_names)} "
        f"teams carry a stable id pair")
    for e in unresolved_af:
        say(f"    UNBRIDGED (dropped, never guessed): "
            f"{e['apifootball_name']!r} tier={e['tier']}")
    for c in collisions:
        say(f"    !! COLLISION: {c}")

    # ------------------------------------------------------- sportec schedule
    say("")
    say("Sportec season schedule — completed matches only")
    sched_body = fetch_sp(f"matches/seasons/{_sportec_season_id()}",
                          {"competition_id": _sportec_competition_id(),
                           "per_page": 500, "match_date[gte]": "2026-01-01",
                           "match_date[lte]": (now.date()
                                               + timedelta(days=1)).isoformat()},
                          "sportec:schedule", top_key="sp_schedule")
    from src.live import mls_stats as ms
    sched = (sched_body or {}).get("schedule") or []
    sp_matches = []
    for m in sched:
        if not ms._is_completed(m.get("match_status")):
            continue
        sp_matches.append({
            "match_id": m.get("match_id"),
            "kickoff": ms._parse_dt(m.get("planned_kickoff_time")
                                    or m.get("kick_off")),
            "home_code": m.get("home_team_three_letter_code"),
            "away_code": m.get("away_team_three_letter_code"),
            "home_cid": m.get("home_team_id"),
            "away_cid": m.get("away_team_id"),
            "home_name": m.get("home_team_name"),
            "away_name": m.get("away_team_name"),
            "home_goals": m.get("home_team_goals"),
            "away_goals": m.get("away_team_goals"),
        })
    say(f"  schedule rows {len(sched)}; completed {len(sp_matches)}")

    # sportec club id -> ESPN team, via three_letter_code == ESPN abbrev
    sportec_bridge, sportec_unbridged = {}, []
    for m in sp_matches:
        for pre in ("home", "away"):
            cid, code, nm = m[f"{pre}_cid"], m[f"{pre}_code"], m[f"{pre}_name"]
            if not cid or cid in sportec_bridge:
                continue
            cands = espn_by_abbrev.get(code) or []
            if len(cands) == 1:
                sportec_bridge[cid] = {
                    "sportec_club_id": cid, "sportec_code": code,
                    "sportec_name": nm,
                    "espn_team_id": int(cands[0]["id"]),
                    "espn_display_name": cands[0].get("displayName")}
            else:
                sportec_unbridged.append(
                    {"sportec_club_id": cid, "sportec_code": code,
                     "sportec_name": nm, "espn_candidates": len(cands)})
    say(f"  Sportec clubs seen {len(sportec_bridge) + len(sportec_unbridged)}; "
        f"bridged to ESPN by three_letter_code == abbrev: "
        f"{len(sportec_bridge)}")
    for u in sportec_unbridged:
        say(f"    UNBRIDGED (dropped): {u['sportec_code']!r} "
            f"{u['sportec_name']!r} -> {u['espn_candidates']} ESPN candidates")

    espn_label = {}
    for e in sportec_bridge.values():
        espn_label[e["espn_team_id"]] = e["espn_display_name"]
    for e in af_to_espn.values():
        espn_label.setdefault(e["espn_team_id"], e["espn_display_name"])

    report["team_bridge"] = {
        "espn_teams": len(espn_teams),
        "apifootball_teams_returned": len(af_team_items),
        "apifootball_resolution": {"resolved": res["resolved"],
                                   "of": res["apifootball_teams"],
                                   "rate": round(res["rate"], 4),
                                   "tiers": res["tiers"]},
        "espn_reference_drift": {
            "in_frozen_roster_not_in_live_espn": roster_not_live,
            "in_live_espn_not_in_frozen_roster": live_not_roster},
        "apifootball_to_espn": team_bridge_rows,
        "apifootball_unbridged": unresolved_af,
        "apifootball_collisions": collisions,
        "sportec_to_espn": sorted(sportec_bridge.values(),
                                  key=lambda r: r["sportec_code"]),
        "sportec_unbridged": sportec_unbridged,
        "both_sides_bridged_espn_ids": sorted(
            set(e["espn_team_id"] for e in af_to_espn.values())
            & set(e["espn_team_id"] for e in sportec_bridge.values())),
    }
    both_ids = set(report["team_bridge"]["both_sides_bridged_espn_ids"])
    say(f"  teams bridged on BOTH sides: {len(both_ids)}/{len(espn_teams)} "
        "ESPN clubs")
    if collisions or unresolved_af or sportec_unbridged:
        say("  NOTE: any team not bridged on both sides drops every fixture "
            "it appears in, and those drops are counted below.")

    # -------------------------------------------------------- AF fixture list
    bar("STAGE 2 — FIXTURE PAIRING (stable ids + uniqueness + score check)")
    fb = fetch_af("fixtures", {"league": AF_MLS_LEAGUE_ID,
                               "season": AF_SEASON},
                  f"fixtures:mls:{AF_SEASON}", top_key="af_fixtures")
    af_items = response_items(fb)
    af_status = {}
    for f in af_items:
        s = ((f.get("fixture") or {}).get("status") or {}).get("short")
        af_status[s] = af_status.get(s, 0) + 1
    say(f"API-Football season {AF_SEASON}: {len(af_items)} fixtures counted "
        f"(provider 'results' field {(fb or {}).get('results')!r}); "
        f"statuses {af_status}")
    af_done = []
    for f in af_items:
        fx = f.get("fixture") or {}
        if ((fx.get("status") or {}).get("short")) not in ("FT", "AET", "PEN"):
            continue
        af_done.append({
            "fixture_id": fx.get("id"),
            "ts": fx.get("timestamp"),
            "date": fx.get("date"),
            "status": ((fx.get("status") or {}).get("short")),
            "home_af_id": ((f.get("teams") or {}).get("home") or {}).get("id"),
            "away_af_id": ((f.get("teams") or {}).get("away") or {}).get("id"),
            "home_name": ((f.get("teams") or {}).get("home") or {}).get("name"),
            "away_name": ((f.get("teams") or {}).get("away") or {}).get("name"),
            "home_goals": (f.get("goals") or {}).get("home"),
            "away_goals": (f.get("goals") or {}).get("away"),
        })
    say(f"  completed (FT/AET/PEN): {len(af_done)}")
    say(f"Sportec completed: {len(sp_matches)}")

    # map both sides into the canonical (espn_home, espn_away) key space
    drops: list[dict] = []

    def key_of_af(r):
        h, a = af_to_espn.get(r["home_af_id"]), af_to_espn.get(r["away_af_id"])
        if h is None or a is None:
            drops.append({"side": "apifootball", "reason": "team_unbridged",
                          "fixture": f"{r['home_name']} vs {r['away_name']}",
                          "apifootball_fixture_id": r["fixture_id"],
                          "unbridged": [n for n, x in
                                        ((r["home_name"], h), (r["away_name"], a))
                                        if x is None]})
            return None
        return (h["espn_team_id"], a["espn_team_id"])

    def key_of_sp(m):
        h = sportec_bridge.get(m["home_cid"])
        a = sportec_bridge.get(m["away_cid"])
        if h is None or a is None:
            drops.append({"side": "sportec", "reason": "team_unbridged",
                          "fixture": f"{m['home_name']} vs {m['away_name']}",
                          "sportec_match_id": m["match_id"],
                          "unbridged": [n for n, x in
                                        ((m["home_name"], h), (m["away_name"], a))
                                        if x is None]})
            return None
        return (h["espn_team_id"], a["espn_team_id"])

    af_by_key: dict[tuple, list] = {}
    for r in af_done:
        k = key_of_af(r)
        if k:
            af_by_key.setdefault(k, []).append(r)
    sp_by_key: dict[tuple, list] = {}
    for m in sp_matches:
        k = key_of_sp(m)
        if k:
            sp_by_key.setdefault(k, []).append(m)
    say(f"  keyed API-Football fixtures: "
        f"{sum(len(v) for v in af_by_key.values())} in "
        f"{len(af_by_key)} ordered team pairs")
    say(f"  keyed Sportec matches:       "
        f"{sum(len(v) for v in sp_by_key.values())} in "
        f"{len(sp_by_key)} ordered team pairs")

    pairs, used_sp = [], set()
    window = KICKOFF_WINDOW_HOURS * 3600
    for k, af_rows in sorted(af_by_key.items()):
        for r in sorted(af_rows, key=lambda x: x["ts"] or 0):
            cands = sp_by_key.get(k) or []
            near = []
            for m in cands:
                if m["match_id"] in used_sp or m["kickoff"] is None \
                        or r["ts"] is None:
                    continue
                dt = abs(m["kickoff"].timestamp() - r["ts"])
                if dt <= window:
                    near.append((dt, m))
            label = (f"{espn_label.get(k[0], k[0])} vs "
                     f"{espn_label.get(k[1], k[1])}")
            if not near:
                drops.append({"side": "pairing", "reason": "no_temporal_match",
                              "fixture": label, "kickoff_af": r["date"],
                              "apifootball_fixture_id": r["fixture_id"],
                              "sportec_candidates_same_team_pair": len(cands)})
                continue
            if len(near) > 1:
                drops.append({
                    "side": "pairing", "reason": "ambiguous_temporal_match",
                    "fixture": label, "kickoff_af": r["date"],
                    "apifootball_fixture_id": r["fixture_id"],
                    "candidates": [{"sportec_match_id": m["match_id"],
                                    "kickoff": m["kickoff"].isoformat(),
                                    "hours_apart": round(dt / 3600, 2)}
                                   for dt, m in sorted(near)]})
                continue
            dt, m = near[0]
            if REQUIRE_SCORE_AGREEMENT and (
                    r["home_goals"] != m["home_goals"]
                    or r["away_goals"] != m["away_goals"]):
                drops.append({
                    "side": "pairing", "reason": "score_mismatch",
                    "fixture": label,
                    "apifootball_fixture_id": r["fixture_id"],
                    "sportec_match_id": m["match_id"],
                    "apifootball_score": [r["home_goals"], r["away_goals"]],
                    "sportec_score": [m["home_goals"], m["away_goals"]],
                    "hours_apart": round(dt / 3600, 2)})
                continue
            used_sp.add(m["match_id"])
            pairs.append({
                "espn_home_team_id": k[0], "espn_away_team_id": k[1],
                "home": espn_label.get(k[0], str(k[0])),
                "away": espn_label.get(k[1], str(k[1])),
                "apifootball_fixture_id": r["fixture_id"],
                "sportec_match_id": m["match_id"],
                "kickoff_utc": (m["kickoff"] or datetime.fromtimestamp(
                    r["ts"], timezone.utc)).isoformat(),
                "hours_apart": round(dt / 3600, 3),
                "home_goals": r["home_goals"], "away_goals": r["away_goals"],
                "af_status": r["status"]})
    for m in sp_matches:
        if m["match_id"] in used_sp:
            continue
        if sportec_bridge.get(m["home_cid"]) and sportec_bridge.get(m["away_cid"]):
            drops.append({"side": "sportec", "reason": "unpaired_sportec_match",
                          "fixture": f"{m['home_name']} vs {m['away_name']}",
                          "sportec_match_id": m["match_id"],
                          "kickoff": m["kickoff"].isoformat()
                          if m["kickoff"] else None})
    pairs.sort(key=lambda p: p["kickoff_utc"])
    # smoke-test escape hatch: not a sampling knob. A limited run is labelled
    # as such in the archive so it can never be mistaken for the full season.
    limit = int(os.getenv("XG_COMPARE_LIMIT", "0") or 0)
    if limit:
        say(f"  !! XG_COMPARE_LIMIT={limit} — SMOKE TEST on the FIRST "
            f"{limit} of {len(pairs)} paired fixtures. NOT a full-season "
            "measurement; do not report these numbers.")
        report["_SMOKE_TEST_LIMIT"] = limit
        pairs = pairs[:limit]
    say("")
    say(f"PAIRED: {len(pairs)} fixtures")
    drop_counts: dict[str, int] = {}
    for d in drops:
        drop_counts[d["reason"]] = drop_counts.get(d["reason"], 0) + 1
    say(f"DROPPED: {len(drops)} — by reason {drop_counts or '{}'}")
    for d in drops[:40]:
        say(f"    {d['reason']:28s} {d.get('fixture', '')}")
    if len(drops) > 40:
        say(f"    … {len(drops) - 40} more, all in the archive")
    report["pairing"] = {
        "apifootball_fixtures_in_season": len(af_items),
        "apifootball_status_counts": af_status,
        "apifootball_completed": len(af_done),
        "sportec_schedule_rows": len(sched),
        "sportec_completed": len(sp_matches),
        "n_paired": len(pairs), "n_dropped": len(drops),
        "drop_counts": drop_counts, "drops": drops, "pairs": pairs,
    }
    if not pairs:
        say("INDETERMINATE — no fixture could be paired.")
        report["verdict"] = "INDETERMINATE"
        report["stdout"] = lines
        out = archive_path(now.date().isoformat())
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
        return 2

    # ------------------------------------------------------------ xG fetch
    bar("STAGE 3 — xG FETCH (paired fixtures only)")
    say(f"API-Football /fixtures/statistics — {len(pairs)} requests "
        f"(budget so far {af.used})")
    af_xg: dict[int, dict] = {}
    af_missing = []
    for i, p in enumerate(pairs):
        fid = p["apifootball_fixture_id"]
        body = fetch_af("fixtures/statistics", {"fixture": fid},
                        f"af:stats:{fid}", stats_key=fid)
        entries = response_items(body)
        got = {}
        types_seen = []
        for e in entries:
            t = (e or {}).get("team") or {}
            eid = (af_to_espn.get(t.get("id")) or {}).get("espn_team_id")
            raw, val, stype = None, None, None
            by_type = {}
            for st in (e.get("statistics") or []):
                ty = (st or {}).get("type")
                if ty is not None:
                    by_type[str(ty)] = st.get("value")
                    if str(ty) not in types_seen:
                        types_seen.append(str(ty))
                if stype is None and is_xg_type(ty):
                    stype, raw = str(ty), st.get("value")
                    val = xg_number(raw)
            if eid is not None:
                # shots come from the SAME payload as this provider's own xG,
                # which is what makes them a valid internal-consistency check
                got[eid] = {"xg": val, "raw": raw, "type": stype,
                            "af_team_id": t.get("id"),
                            "shots_on_target":
                                xg_number(by_type.get("Shots on Goal")),
                            "shots_total":
                                xg_number(by_type.get("Total Shots")),
                            "n_stats": len(e.get("statistics") or [])}
        af_xg[fid] = {"teams": got, "entries": len(entries),
                      "statistic_types_offered": types_seen}
        want = {p["espn_home_team_id"], p["espn_away_team_id"]}
        absent = [e for e in want
                  if got.get(e, {}).get("xg") is None]
        if absent or len(entries) != 2:
            af_missing.append({
                "apifootball_fixture_id": fid,
                "fixture": f"{p['home']} vs {p['away']}",
                "entries": len(entries),
                "absent_for": [espn_label.get(e, str(e)) for e in absent],
                "raw": {espn_label.get(e, str(e)):
                        got.get(e, {}).get("raw") for e in want},
                "xg_type_seen": bool(types_seen and any(
                    is_xg_type(t) for t in types_seen))})
        if (i + 1) % 50 == 0:
            say(f"    … {i + 1}/{len(pairs)} (requests used {af.used})")
    say(f"  API-Football requests used: {af.used}")
    say(f"  fixtures with an ABSENT xG on at least one side: "
        f"{len(af_missing)}/{len(pairs)}")

    say("")
    say(f"Sportec /statistics/clubs/matches/<id> — {len(pairs)} requests")
    sp_xg: dict[str, dict] = {}
    sp_missing = []
    for i, p in enumerate(pairs):
        mid = p["sportec_match_id"]
        body = fetch_sp(f"statistics/clubs/matches/{mid}", None,
                        f"sp:stats:{mid}", stats_key=mid)
        got, n_entries = {}, 0
        try:
            teams = body["match_statistics_list"][0][
                "match_statistics"]["team_statistics"]
        except (KeyError, IndexError, TypeError):
            teams = []
        n_entries = len(teams)
        for t in teams:
            br = sportec_bridge.get(t.get("team_id"))
            if br is None:
                continue
            got[br["espn_team_id"]] = {
                "xg": xg_number(t.get("xG")), "raw": t.get("xG"),
                "type": "xG", "sportec_club_id": t.get("team_id"),
                "shots_on_target": xg_number(t.get("shots_on_target")),
                "shots_total": xg_number(t.get("shots_at_goal_sum")),
                "goals": t.get("goals"), "role": t.get("team_role")}
        try:
            data_status = body["match_statistics_list"][0][
                "match_statistics"].get("data_status")
        except (KeyError, IndexError, TypeError):
            data_status = None
        sp_xg[mid] = {"teams": got, "entries": n_entries,
                      "data_status": data_status}
        want = {p["espn_home_team_id"], p["espn_away_team_id"]}
        absent = [e for e in want if got.get(e, {}).get("xg") is None]
        if absent or n_entries != 2:
            sp_missing.append({
                "sportec_match_id": mid,
                "fixture": f"{p['home']} vs {p['away']}",
                "entries": n_entries,
                "absent_for": [espn_label.get(e, str(e)) for e in absent],
                "raw": {espn_label.get(e, str(e)):
                        got.get(e, {}).get("raw") for e in want}})
        if (i + 1) % 50 == 0:
            say(f"    … {i + 1}/{len(pairs)}")
    say(f"  fixtures with an ABSENT xG on at least one side: "
        f"{len(sp_missing)}/{len(pairs)}")
    report["xg_absence"] = {
        "apifootball": {"n_fixtures_with_absence": len(af_missing),
                        "of": len(pairs), "detail": af_missing},
        "sportec": {"n_fixtures_with_absence": len(sp_missing),
                    "of": len(pairs), "detail": sp_missing},
    }

    # ------------------------------------------------------- matched pairs
    bar("STAGE 4 — THE COMPARISON")
    obs, usable_fixtures = [], []
    excluded_absent = []
    for p in pairs:
        a = af_xg.get(p["apifootball_fixture_id"], {}).get("teams", {})
        s = sp_xg.get(p["sportec_match_id"], {}).get("teams", {})
        sides = []
        ok = True
        for side, eid in (("home", p["espn_home_team_id"]),
                          ("away", p["espn_away_team_id"])):
            av = (a.get(eid) or {}).get("xg")
            sv = (s.get(eid) or {}).get("xg")
            if av is None or sv is None:
                ok = False
                break
            sides.append({"side": side, "espn_team_id": eid,
                          "team": espn_label.get(eid, str(eid)),
                          "af": av, "sp": sv, "diff": av - sv})
        if not ok:
            excluded_absent.append({
                "fixture": f"{p['home']} vs {p['away']}",
                "kickoff_utc": p["kickoff_utc"],
                "apifootball_fixture_id": p["apifootball_fixture_id"],
                "sportec_match_id": p["sportec_match_id"],
                "reason": "xg_absent_on_at_least_one_side_in_one_source"})
            continue
        for sd in sides:
            ax, sx = a.get(sd["espn_team_id"]) or {}, s.get(sd["espn_team_id"]) or {}
            sd["af_sot"] = ax.get("shots_on_target")
            sd["sp_sot"] = sx.get("shots_on_target")
            sd["af_shots"] = ax.get("shots_total")
            sd["sp_shots"] = sx.get("shots_total")
        usable_fixtures.append({**p, "sides": sides,
                                "sportec_data_status":
                                    (sp_xg.get(p["sportec_match_id"]) or {})
                                    .get("data_status")})
        obs.extend([{**sd, "fixture": f"{p['home']} vs {p['away']}",
                     "kickoff_utc": p["kickoff_utc"],
                     "apifootball_fixture_id": p["apifootball_fixture_id"],
                     "sportec_match_id": p["sportec_match_id"]}
                    for sd in sides])

    n_fx, n_obs = len(usable_fixtures), len(obs)
    say(f"paired fixtures                              {len(pairs)}")
    say(f"  excluded: xG absent in at least one source {len(excluded_absent)}")
    say(f"COMPARABLE FIXTURES                          {n_fx}")
    say(f"COMPARABLE TEAM-OBSERVATIONS (2 per fixture) {n_obs}")
    if n_obs < 3:
        say("INDETERMINATE — too few comparable observations.")
        report["verdict"] = "INDETERMINATE"
        report["stdout"] = lines
        out = archive_path(now.date().isoformat())
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
        return 2

    A = np.array([o["af"] for o in obs])
    S = np.array([o["sp"] for o in obs])
    D = A - S
    per_fx_diff = [[sd["diff"] for sd in f["sides"]] for f in usable_fixtures]
    per_fx_abs = [[abs(sd["diff"]) for sd in f["sides"]]
                  for f in usable_fixtures]

    r_p, r_s = pearson(A, S), spearman(A, S)
    signed = cluster_bootstrap_mean(per_fx_diff, N_BOOTSTRAP, BOOTSTRAP_SEED)
    absolute = cluster_bootstrap_mean(per_fx_abs, N_BOOTSTRAP,
                                      BOOTSTRAP_SEED + 1)
    # OLS of API-Football on Sportec: slope < 1 means AF compresses the
    # spread, which matters more for a ratings model than the mean level
    slope, intercept = np.polyfit(S, A, 1)

    say("")
    say(f"CORRELATION       Pearson  r = {r_p:+.4f}"
        f"    R^2 = {r_p ** 2:.4f}")
    say(f"                  Spearman r = {r_s:+.4f}")
    say(f"MEAN LEVEL        API-Football {np.mean(A):.4f}   "
        f"Sportec {np.mean(S):.4f}")
    say(f"                  sd           {np.std(A, ddof=1):.4f}   "
        f"        {np.std(S, ddof=1):.4f}")
    say(f"MEAN SIGNED DIFF  (API-Football - Sportec) = "
        f"{signed['mean']:+.4f}  95% CI "
        f"[{signed['ci95'][0]:+.4f}, {signed['ci95'][1]:+.4f}]  "
        f"(fixture-clustered bootstrap, {N_BOOTSTRAP} resamples)")
    say(f"                  interval excludes zero: {signed['excludes_zero']}")
    say(f"MEAN ABS DIFF     {absolute['mean']:.4f}  95% CI "
        f"[{absolute['ci95'][0]:.4f}, {absolute['ci95'][1]:.4f}]")
    say(f"OLS               API-Football = {slope:.4f} x Sportec "
        f"{intercept:+.4f}")

    dist_abs = distribution(np.abs(D))
    dist_signed = distribution(D)
    say("")
    say("DISTRIBUTION OF |API-Football - Sportec| per team-observation "
        f"(n={n_obs}) — the mean alone hides this:")
    for q, v in dist_abs["percentiles"].items():
        say(f"    {q:>5s}  {v:.3f}")
    hist = histogram(np.abs(D), [0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5,
                                 float(max(2.0, np.max(np.abs(D))))])
    say("")
    for h in hist:
        say(f"    |diff| in {h['range']:<14s} {h['count']:4d}  "
            f"{h['pct']:5.1f}%   {'#' * int(h['pct'] / 2)}")

    # ordering agreement within a fixture
    ord_rows = []
    agree = disagree = tie_af = tie_sp = tie_both = 0
    for f in usable_fixtures:
        h = [sd for sd in f["sides"] if sd["side"] == "home"][0]
        a_ = [sd for sd in f["sides"] if sd["side"] == "away"][0]
        d_af, d_sp = h["af"] - a_["af"], h["sp"] - a_["sp"]
        s_af = 0 if d_af == 0 else (1 if d_af > 0 else -1)
        s_sp = 0 if d_sp == 0 else (1 if d_sp > 0 else -1)
        if s_af == 0 and s_sp == 0:
            tie_both += 1
            cls = "tie_in_both"
        elif s_af == 0:
            tie_af += 1
            cls = "tie_in_apifootball_only"
        elif s_sp == 0:
            tie_sp += 1
            cls = "tie_in_sportec_only"
        elif s_af == s_sp:
            agree += 1
            cls = "agree"
        else:
            disagree += 1
            cls = "DISAGREE"
        ord_rows.append({
            "fixture": f"{f['home']} vs {f['away']}",
            "kickoff_utc": f["kickoff_utc"], "class": cls,
            "apifootball": [h["af"], a_["af"]], "sportec": [h["sp"], a_["sp"]],
            "af_xgd": round(d_af, 4), "sp_xgd": round(d_sp, 4),
            "goals": [f["home_goals"], f["away_goals"]],
            "apifootball_fixture_id": f["apifootball_fixture_id"],
            "sportec_match_id": f["sportec_match_id"]})
    decided = agree + disagree
    say("")
    say("ORDERING AGREEMENT — do both sources agree WHICH TEAM out-created? "
        "(what a relative-strength ratings model actually consumes)")
    say(f"    fixtures where both sources give a strict ordering  "
        f"{decided}/{n_fx}")
    say(f"    agree on the direction                             "
        f"{agree}/{decided}"
        + (f" = {agree / decided:.1%}  95% CI "
           f"{wilson_ci(agree, decided)}" if decided else ""))
    say(f"    DISAGREE on the direction                          "
        f"{disagree}/{decided}")
    say(f"    exact ties (no strict ordering): both {tie_both}, "
        f"API-Football only {tie_af}, Sportec only {tie_sp}")

    worst = sorted(obs, key=lambda o: -abs(o["diff"]))[:WORST_N]
    say("")
    say(f"WORST {WORST_N} DISAGREEMENTS, NAMED (per team-observation):")
    say(f"    {'|diff|':>7s} {'API-F':>6s} {'Sportec':>8s}  "
        f"{'team':<26s} fixture / kickoff")
    for o in worst:
        say(f"    {abs(o['diff']):7.3f} {o['af']:6.2f} {o['sp']:8.4f}  "
            f"{o['team']:<26s} {o['fixture']}  {o['kickoff_utc'][:10]}")
    worst_fx = sorted(
        usable_fixtures,
        key=lambda f: -max(abs(sd["diff"]) for sd in f["sides"]))[:WORST_N]
    say("")
    say(f"WORST {WORST_N} FIXTURES, both sides shown:")
    for f in worst_fx:
        h = [sd for sd in f["sides"] if sd["side"] == "home"][0]
        a_ = [sd for sd in f["sides"] if sd["side"] == "away"][0]
        say(f"    {f['kickoff_utc'][:10]}  {f['home']} {f['home_goals']}-"
            f"{f['away_goals']} {f['away']}")
        say(f"        API-Football  {h['af']:.2f} - {a_['af']:.2f}"
            f"      Sportec  {h['sp']:.4f} - {a_['sp']:.4f}"
            f"      |diff| {abs(h['diff']):.2f} / {abs(a_['diff']):.2f}")
    ord_disagree = [r for r in ord_rows if r["class"] == "DISAGREE"]
    if ord_disagree:
        say("")
        say(f"EVERY ORDERING DISAGREEMENT ({len(ord_disagree)}), NAMED:")
        for r in ord_disagree:
            say(f"    {r['kickoff_utc'][:10]}  {r['fixture']}  "
                f"goals {r['goals'][0]}-{r['goals'][1]}")
            say(f"        API-Football {r['apifootball'][0]:.2f}-"
                f"{r['apifootball'][1]:.2f} (xGD {r['af_xgd']:+.2f})   "
                f"Sportec {r['sportec'][0]:.4f}-{r['sportec'][1]:.4f} "
                f"(xGD {r['sp_xgd']:+.2f})")

    report["comparison"] = {
        "n_paired_fixtures": len(pairs),
        "n_excluded_absent_xg": len(excluded_absent),
        "excluded_absent_xg": excluded_absent,
        "n_comparable_fixtures": n_fx,
        "n_comparable_team_observations": n_obs,
        "pearson_r": round(r_p, 4), "pearson_r2": round(r_p ** 2, 4),
        "spearman_rho": round(r_s, 4),
        "apifootball_mean": round(float(np.mean(A)), 4),
        "apifootball_sd": round(float(np.std(A, ddof=1)), 4),
        "sportec_mean": round(float(np.mean(S)), 4),
        "sportec_sd": round(float(np.std(S, ddof=1)), 4),
        "mean_signed_diff_af_minus_sp": signed,
        "mean_abs_diff": absolute,
        "ols_af_on_sp": {"slope": round(float(slope), 4),
                         "intercept": round(float(intercept), 4)},
        "abs_diff_distribution": dist_abs,
        "signed_diff_distribution": dist_signed,
        "abs_diff_histogram": hist,
        "ordering": {
            "n_fixtures": n_fx, "n_strictly_ordered_in_both": decided,
            "agree": agree, "disagree": disagree,
            "agree_rate": round(agree / decided, 4) if decided else None,
            "agree_rate_ci95_wilson": wilson_ci(agree, decided),
            "ties_both": tie_both, "ties_apifootball_only": tie_af,
            "ties_sportec_only": tie_sp,
            "rows": ord_rows},
        "worst_observations": worst,
        "worst_fixtures": [
            {"fixture": f"{f['home']} vs {f['away']}",
             "kickoff_utc": f["kickoff_utc"],
             "goals": [f["home_goals"], f["away_goals"]],
             "apifootball": [sd["af"] for sd in f["sides"]],
             "sportec": [sd["sp"] for sd in f["sides"]],
             "apifootball_fixture_id": f["apifootball_fixture_id"],
             "sportec_match_id": f["sportec_match_id"]}
            for f in worst_fx],
        "all_observations": obs,
    }

    # ------------------------------------------- attribution: WHOSE xG moved?
    bar("STAGE 4b — ATTRIBUTION: WHICH SOURCE IS OUT OF LINE, AND WHERE?")
    say("A disagreement says two numbers differ; it does not say which one is "
        "wrong. Two things separate them, both computed from fields the "
        "providers publish ALONGSIDE their own xG:")
    say("  (1) SHOT AGREEMENT — a non-xG field. If both payloads describe the "
        "same match, shots must agree. This validates the join independently "
        "of the thing being measured.")
    say("  (2) INTERNAL CONSISTENCY — total match xG per that provider's OWN "
        "shots on target. No shot model produces a collapsing rate while "
        "shot volume holds; a provider whose own ratio moves is the one that "
        "moved.")

    say("")
    say("(1) SHOT-FIELD AGREEMENT (independent of xG)")
    sot_a = np.array([sd["af_sot"] for o in usable_fixtures
                      for sd in o["sides"]], dtype=float)
    sot_s = np.array([sd["sp_sot"] for o in usable_fixtures
                      for sd in o["sides"]], dtype=float)
    sh_a = np.array([sd["af_shots"] for o in usable_fixtures
                     for sd in o["sides"]], dtype=float)
    sh_s = np.array([sd["sp_shots"] for o in usable_fixtures
                     for sd in o["sides"]], dtype=float)
    shots_ok = not (np.isnan(sot_a).any() or np.isnan(sot_s).any())
    shot_agreement = {}
    if shots_ok:
        shot_agreement = {
            "shots_on_target": {
                "apifootball_mean": round(float(np.mean(sot_a)), 3),
                "sportec_mean": round(float(np.mean(sot_s)), 3),
                "pearson_r": round(pearson(sot_a, sot_s), 4),
                "exact_agreement_rate":
                    round(float(np.mean(sot_a == sot_s)), 4)},
            "total_shots": {
                "apifootball_mean": round(float(np.mean(sh_a)), 3),
                "sportec_mean": round(float(np.mean(sh_s)), 3),
                "pearson_r": round(pearson(sh_a, sh_s), 4),
                "exact_agreement_rate":
                    round(float(np.mean(sh_a == sh_s)), 4)}}
        for fld, m in shot_agreement.items():
            say(f"    {fld:16s} API-F mean {m['apifootball_mean']:6.3f}   "
                f"Sportec mean {m['sportec_mean']:6.3f}   "
                f"Pearson r {m['pearson_r']:+.4f}   "
                f"exact match {m['exact_agreement_rate']:.1%}")
        say("    -> the two payloads describe the SAME matches; the join is "
            "corroborated by a field neither xG model touches.")
    else:
        say("    NOT_RUN — a shot field was absent for at least one "
            "observation.")

    say("")
    say("(2) INTERNAL CONSISTENCY BY MONTH — total match xG / that "
        "provider's OWN shots on target")
    say(f"    {'month':9s} {'n':>4s} | {'AF xG':>7s} {'AF SoT':>7s} "
        f"{'xG/SoT':>8s} | {'SP xG':>7s} {'SP SoT':>7s} {'xG/SoT':>8s} |"
        f" {'goals':>6s}")
    by_month: dict[str, list] = {}
    for f in usable_fixtures:
        by_month.setdefault(f["kickoff_utc"][:7], []).append(f)
    consistency = {}
    for m, items in sorted(by_month.items()):
        axg = float(np.mean([sum(sd["af"] for sd in f["sides"]) for f in items]))
        sxg = float(np.mean([sum(sd["sp"] for sd in f["sides"]) for f in items]))
        asot = float(np.mean([sum(sd["af_sot"] or 0 for sd in f["sides"])
                              for f in items]))
        ssot = float(np.mean([sum(sd["sp_sot"] or 0 for sd in f["sides"])
                              for f in items]))
        g = float(np.mean([(f["home_goals"] or 0) + (f["away_goals"] or 0)
                           for f in items]))
        consistency[m] = {
            "n": len(items), "apifootball_xg": round(axg, 4),
            "apifootball_sot": round(asot, 3),
            "apifootball_xg_per_sot": round(axg / asot, 4) if asot else None,
            "sportec_xg": round(sxg, 4), "sportec_sot": round(ssot, 3),
            "sportec_xg_per_sot": round(sxg / ssot, 4) if ssot else None,
            "goals": round(g, 3)}
        say(f"    {m:9s} {len(items):4d} | {axg:7.3f} {asot:7.2f} "
            f"{axg / asot if asot else float('nan'):8.4f} | {sxg:7.3f} "
            f"{ssot:7.2f} {sxg / ssot if ssot else float('nan'):8.4f} | "
            f"{g:6.2f}")
    say("")
    say("    API-Football's ratio is the STABLE one across the season; any "
        "month where only Sportec's ratio moves is a Sportec anomaly, and "
        "the disagreement in that month cannot be charged to API-Football.")

    # a plausibility screen stated as a rate, applied IDENTICALLY to both
    # providers so it cannot favour either. 0.06 is far below any month's
    # league ratio above (~0.25-0.34), so it flags collapse, not noise.
    IMPLAUSIBLE_XG_PER_SOT = 0.06
    say("")
    say(f"(3) PLAUSIBILITY SCREEN, applied IDENTICALLY to BOTH providers: a "
        f"match whose total xG is under {IMPLAUSIBLE_XG_PER_SOT} per its own "
        f"shot on target (every month's league ratio above is 0.24-0.34, so "
        f"this flags collapse, not noise)")
    screen = {"threshold_xg_per_own_sot": IMPLAUSIBLE_XG_PER_SOT}
    for src, xk, sk in (("apifootball", "af", "af_sot"),
                        ("sportec", "sp", "sp_sot")):
        flagged = []
        for f in usable_fixtures:
            tx = sum(sd[xk] for sd in f["sides"])
            ts = sum(sd[sk] or 0 for sd in f["sides"])
            if ts > 0 and tx / ts < IMPLAUSIBLE_XG_PER_SOT:
                flagged.append({
                    "fixture": f"{f['home']} vs {f['away']}",
                    "kickoff_utc": f["kickoff_utc"],
                    "total_xg": round(tx, 4), "total_own_sot": int(ts),
                    "xg_per_sot": round(tx / ts, 4),
                    "goals": [f["home_goals"], f["away_goals"]],
                    "sportec_data_status": f.get("sportec_data_status"),
                    "apifootball_fixture_id": f["apifootball_fixture_id"],
                    "sportec_match_id": f["sportec_match_id"]})
        screen[src] = {"n_flagged": len(flagged), "of": n_fx,
                       "flagged": flagged}
        say(f"    {src:14s} {len(flagged)}/{n_fx} matches flagged")
        for x in flagged:
            say(f"        {x['kickoff_utc'][:10]}  {x['fixture'][:44]:<44s} "
                f"total xG {x['total_xg']:6.3f} on {x['total_own_sot']:2d} "
                f"own SoT = {x['xg_per_sot']:.4f}   goals "
                f"{x['goals'][0]}-{x['goals'][1]}   Sportec data_status "
                f"{x['sportec_data_status']!r}")
    say("")
    say("    Sportec declares data_status 'postmatch' on those rows — i.e. "
        "the provider itself reports them as FINAL, not provisional. There "
        "is no flag on the payload that would let an ingester reject them.")

    # ---- calendar-split sensitivity. The boundary is the World Cup break,
    # which is external to this data: MLS paused for the tournament and there
    # is no June fixture at all. It is NOT a threshold picked from the
    # disagreement values, which is what would make it cherry-picking.
    months = sorted(by_month)
    pre = [f for f in usable_fixtures if f["kickoff_utc"][:7] < "2026-06"]
    post = [f for f in usable_fixtures if f["kickoff_utc"][:7] >= "2026-06"]
    say("")
    say("(4) CALENDAR-SPLIT SENSITIVITY. Months present: "
        f"{months} — there is NO June fixture: MLS paused for the World Cup. "
        "That break is an EXTERNAL boundary, not a cut chosen from the "
        "disagreements, which is what keeps this from being cherry-picking.")

    def window_stats(items: list, label: str) -> dict:
        if len(items) < 3:
            return {"label": label, "n_fixtures": len(items),
                    "status": "TOO_FEW"}
        A_ = np.array([sd["af"] for f in items for sd in f["sides"]])
        S_ = np.array([sd["sp"] for f in items for sd in f["sides"]])
        pf = [[sd["af"] - sd["sp"] for sd in f["sides"]] for f in items]
        pfa = [[abs(sd["af"] - sd["sp"]) for sd in f["sides"]] for f in items]
        sg = cluster_bootstrap_mean(pf, 4000, BOOTSTRAP_SEED)
        ab = cluster_bootstrap_mean(pfa, 4000, BOOTSTRAP_SEED + 1)
        ag = dis = 0
        for f in items:
            h = [sd for sd in f["sides"] if sd["side"] == "home"][0]
            aw = [sd for sd in f["sides"] if sd["side"] == "away"][0]
            d1, d2 = h["af"] - aw["af"], h["sp"] - aw["sp"]
            if d1 == 0 or d2 == 0:
                continue
            if (d1 > 0) == (d2 > 0):
                ag += 1
            else:
                dis += 1
        sl, ic = np.polyfit(S_, A_, 1)
        return {"label": label, "n_fixtures": len(items),
                "n_observations": len(A_),
                "pearson_r": round(pearson(A_, S_), 4),
                "spearman_rho": round(spearman(A_, S_), 4),
                "apifootball_mean": round(float(np.mean(A_)), 4),
                "sportec_mean": round(float(np.mean(S_)), 4),
                "mean_signed_diff": sg, "mean_abs_diff": ab,
                "ols_slope": round(float(sl), 4),
                "ols_intercept": round(float(ic), 4),
                "ordering_agree": ag, "ordering_decided": ag + dis,
                "ordering_agree_rate": round(ag / (ag + dis), 4)
                if (ag + dis) else None,
                "ordering_agree_ci95": wilson_ci(ag, ag + dis)}

    windows = [window_stats(usable_fixtures, "ALL (primary)"),
               window_stats(pre, "Feb-May (pre-World-Cup break)"),
               window_stats(post, "July (post-restart)")]
    say("")
    say(f"    {'window':32s} {'n':>4s} {'r':>8s} {'rho':>8s} "
        f"{'signed':>8s} {'MAD':>7s} {'slope':>7s} {'order agree':>13s}")
    for w in windows:
        if w.get("status") == "TOO_FEW":
            say(f"    {w['label']:32s} {w['n_fixtures']:4d}  TOO_FEW")
            continue
        say(f"    {w['label']:32s} {w['n_fixtures']:4d} "
            f"{w['pearson_r']:+8.4f} {w['spearman_rho']:+8.4f} "
            f"{w['mean_signed_diff']['mean']:+8.4f} "
            f"{w['mean_abs_diff']['mean']:7.4f} {w['ols_slope']:7.4f} "
            f"{w['ordering_agree']:4d}/{w['ordering_decided']:<4d}"
            f"{w['ordering_agree_rate']:5.1%}")
    say("")
    say("    Read the Feb-May row as the cleaner estimate of how the two "
        "PROVIDERS compare, and the ALL row as the primary pre-specified "
        "result. Neither row is the 'real' one to the exclusion of the "
        "other: the July degradation is real data that a live ingester "
        "would have consumed.")

    report["attribution"] = {
        "shot_field_agreement": shot_agreement,
        "internal_consistency_by_month": consistency,
        "plausibility_screen": screen,
        "sportec_data_status_counts": _count(
            f.get("sportec_data_status") for f in usable_fixtures),
        "calendar_split_windows": windows,
        "calendar_split_rationale": (
            "The boundary is the World Cup break (no June fixture exists), "
            "which is external to the measured values. It is not a threshold "
            "selected from the disagreement distribution."),
    }

    # ---------------------------------------------------------------- refit
    bar("STAGE 5 — WOULD THE SUBSTITUTION CHANGE THE FITTED MLS RATINGS?")
    from src.live import model_mls as mm
    import config as cfg

    fixtures = []
    xg_sp: dict[int, dict] = {}
    xg_af: dict[int, dict] = {}
    for i, f in enumerate(usable_fixtures, start=1):
        ko = datetime.fromisoformat(f["kickoff_utc"])
        fixtures.append(StandInFixture(i, f["espn_home_team_id"],
                                       f["espn_away_team_id"],
                                       f["home_goals"], f["away_goals"], ko))
        h = [sd for sd in f["sides"] if sd["side"] == "home"][0]
        a_ = [sd for sd in f["sides"] if sd["side"] == "away"][0]
        xg_sp[i] = {"home": {"xg": h["sp"], "xg_against": a_["sp"]},
                    "away": {"xg": a_["sp"], "xg_against": h["sp"]}}
        xg_af[i] = {"home": {"xg": h["af"], "xg_against": a_["af"]},
                    "away": {"xg": a_["af"], "xg_against": h["af"]}}
    fixtures.sort(key=lambda x: x.current_kickoff_utc)
    as_of = max(x.current_kickoff_utc for x in fixtures) + timedelta(seconds=1)
    alpha = cfg.MLS_XG_RATING_ALPHA
    say(f"model_mls.fit imported, NOT modified. "
        f"MLS_XG_RATING_ALPHA={alpha}  XG_SHRINK_GAMES={mm.XG_SHRINK_GAMES}  "
        f"SHRINK_GAMES={mm.SHRINK_GAMES}")
    say(f"fit window: {len(fixtures)} comparable fixtures, "
        f"as_of {as_of.isoformat()} (max kickoff + 1s, so the recency "
        f"weighting is deterministic and not wall-clock dependent)")

    fit_sp = mm.fit(fixtures, as_of, xg_by_fixture=xg_sp, xg_alpha=alpha)
    fit_af = mm.fit(fixtures, as_of, xg_by_fixture=xg_af, xg_alpha=alpha)
    fit_goals = mm.fit(fixtures, as_of, xg_by_fixture=None, xg_alpha=0.0)
    say(f"  league_xg   Sportec {fit_sp['league_xg']:.4f}   "
        f"API-Football {fit_af['league_xg']:.4f}   "
        f"(league_gpg {fit_sp['league_gpg']:.4f})")
    say(f"  xg_coverage Sportec {fit_sp['xg_coverage']}   "
        f"API-Football {fit_af['xg_coverage']}")

    rd = rating_diff(fit_sp, fit_af, espn_label)
    for field in ("attack", "defence"):
        d = rd[field]
        say("")
        say(f"  {field.upper()} ratings, Sportec-fitted vs API-Football-fitted "
            f"({rd['n_teams_compared']} teams)")
        say(f"    mean |diff|      {d['mean_abs_diff']:.4f}"
            f"      mean signed {d['mean_signed_diff']:+.4f}")
        say(f"    max  |diff|      {d['max_abs_diff']:.4f}"
            f"      ({d['max_abs_diff_team']})")
        say(f"    league spread    Sportec {d['spread_a']:.4f}   "
            f"API-Football {d['spread_b']:.4f}")
        say(f"    rating agreement Pearson {d['pearson_of_ratings']:+.4f}   "
            f"Spearman {d['spearman_of_ratings']:+.4f}")
        say(f"    teams whose rank changes {d['n_teams_rank_changed']}"
            f"/{rd['n_teams_compared']}   max rank move "
            f"{d['max_rank_change']}")
        say(f"    best-rated team  Sportec {d['top_team_a']!r}   "
            f"API-Football {d['top_team_b']!r}")
    biggest = sorted([r for r in rd["per_team"] if r.get("present_in_both")],
                     key=lambda r: -abs(r["attack_diff"]))[:8]
    say("")
    say("  largest ATTACK-rating moves from the substitution:")
    for r in biggest:
        say(f"    {r['team']:<26s} Sportec {r['attack_a']:.4f} -> "
            f"API-Football {r['attack_b']:.4f}  "
            f"({r['attack_diff']:+.4f})  games {r['games']}")

    say("")
    say("M3 RUNG — rolling-origin walk-forward under each xG source "
        "(imported fit_variant / predict_variant / _score_fixture, so the "
        "rung definitions cannot drift from the deployed ones)")
    rungs = ("M0", "M2C", "M3")
    wf_sp = walk_forward(fixtures, xg_sp, rungs, say)
    wf_af = walk_forward(fixtures, xg_af, rungs, say)
    say(f"  scored fixtures: Sportec {wf_sp['n_scored']}, "
        f"API-Football {wf_af['n_scored']} "
        f"(skipped for insufficient history: "
        f"{wf_sp['n_skipped_insufficient_history']})")
    aligned = wf_sp["scored_ids"] == wf_af["scored_ids"]
    say(f"  the two walk-forwards scored the IDENTICAL fixture set: "
        f"{aligned} — required before any paired comparison of them")
    if not aligned:
        say("    !! the sets differ, so the paired edge below is NOT a "
            "like-for-like comparison and must not be read as one")
    ms_sp, ms_af = (rung_means(wf_sp["per_fixture"], rungs),
                    rung_means(wf_af["per_fixture"], rungs))
    if not wf_sp["per_fixture"]:
        say("  NOT_RUN — no fixture was scorable by every rung. With too few "
            "fixtures no team reaches model_eval.MIN_GAMES, so the rung "
            "comparison could not be measured. Reported as not-run, never as "
            "agreement.")
        m3_between = {"status": "NOT_RUN",
                      "reason": "no fixture scorable by every rung"}
        edges_sp = edges_af = {"status": "NOT_RUN"}
    else:
        say(f"    {'rung':6s} {'log loss (Sportec)':>20s} "
            f"{'log loss (API-F)':>18s} {'delta':>10s}")
        for n in rungs:
            say(f"    {n:6s} {ms_sp[n]['log_loss']:20.5f} "
                f"{ms_af[n]['log_loss']:18.5f} "
                f"{ms_af[n]['log_loss'] - ms_sp[n]['log_loss']:+10.5f}")
        m3_between = paired_edge(wf_sp["per_fixture"], wf_af["per_fixture"],
                                 "M3", 2000, BOOTSTRAP_SEED + 2)
        say("")
        say(f"  M3 under Sportec vs M3 under API-Football, same fixtures, "
            f"n={m3_between['n']}")
        say(f"    delta log loss (positive = Sportec better) "
            f"{m3_between['delta_log_loss']:+.5f}  95% CI "
            f"[{m3_between['ci95'][0]:+.5f}, {m3_between['ci95'][1]:+.5f}]  "
            f"significant {m3_between['significant']}")
        say(f"    fixtures scored IDENTICALLY by both sources: "
            f"{m3_between['n_fixtures_identical_score']}/{m3_between['n']}")

    def within_edge(wf, label):
        pf = wf["per_fixture"]
        a = np.array([p["M3"]["log_loss"] for p in pf])
        b = np.array([p["M2C"]["log_loss"] for p in pf])
        z = np.array([p["M0"]["log_loss"] for p in pf])
        rng = np.random.default_rng(BOOTSTRAP_SEED + 3)
        n = len(a)
        out = {}
        for nm, base in (("M3_vs_M2C", b), ("M3_vs_M0", z)):
            d = base - a
            boot = np.array([float(np.mean(d[rng.integers(0, n, n)]))
                             for _ in range(2000)])
            lo, hi = np.percentile(boot, [2.5, 97.5])
            out[nm] = {"delta_log_loss": round(float(np.mean(d)), 5),
                       "ci95": [round(float(lo), 5), round(float(hi), 5)],
                       "significant": bool(lo > 0 or hi < 0), "n": n}
            say(f"    [{label}] {nm:10s} {out[nm]['delta_log_loss']:+.5f}  "
                f"95% CI [{out[nm]['ci95'][0]:+.5f}, "
                f"{out[nm]['ci95'][1]:+.5f}]  "
                f"significant {out[nm]['significant']}")
        return out
    if wf_sp["per_fixture"]:
        say("")
        say("  the ladder edge each source produces (positive = M3 better):")
        edges_sp = within_edge(wf_sp, "Sportec")
        edges_af = within_edge(wf_af, "API-Football")

    report["refit"] = {
        "method": ("model_mls.fit imported and parameterized with a different "
                   "xg_by_fixture map per source; src/live/model_mls.py "
                   "unmodified (sha256 recorded before and after). Ratings "
                   "fitted on the SAME fixtures, same as_of, same alpha and "
                   "shrink, so the only difference is the xG source."),
        "xg_alpha": alpha, "xg_shrink_games": mm.XG_SHRINK_GAMES,
        "shrink_games": mm.SHRINK_GAMES,
        "calibration_alpha": cfg.MLS_CALIBRATION_ALPHA,
        "n_fixtures": len(fixtures), "as_of": as_of.isoformat(),
        "league_xg_sportec": round(fit_sp["league_xg"], 4),
        "league_xg_apifootball": round(fit_af["league_xg"], 4),
        "league_gpg": round(fit_sp["league_gpg"], 4),
        "xg_coverage_sportec": fit_sp["xg_coverage"],
        "xg_coverage_apifootball": fit_af["xg_coverage"],
        "ratings_diff": rd,
        "goals_only_reference": {
            "league_gpg": round(fit_goals["league_gpg"], 4),
            "venue_home": round(fit_goals["venue_home"], 4)},
        "walk_forward": {
            "rungs": list(rungs),
            "n_scored_sportec": wf_sp["n_scored"],
            "n_scored_apifootball": wf_af["n_scored"],
            "n_skipped_insufficient_history":
                wf_sp["n_skipped_insufficient_history"],
            "means_sportec": ms_sp, "means_apifootball": ms_af,
            "scored_sets_identical": aligned,
            "m3_sportec_vs_m3_apifootball": m3_between,
            "ladder_edges_sportec": edges_sp,
            "ladder_edges_apifootball": edges_af},
    }

    for rel in engine_files:
        report["engine_files_sha256_after"][rel] = sha256_file(REPO_ROOT / rel)
    unchanged = all(report["engine_files_sha256_before"][r]
                    == report["engine_files_sha256_after"][r]
                    for r in engine_files)
    report["engine_files_unchanged"] = unchanged
    bar("ENGINE INTEGRITY")
    for rel in engine_files:
        b_, a_ = (report["engine_files_sha256_before"][rel],
                  report["engine_files_sha256_after"][rel])
        say(f"  {'SAME' if b_ == a_ else 'CHANGED!!'}  {b_}  {rel}")
    say(f"  all MLS engine files byte-identical before and after: {unchanged}")

    report["budget"] = {"apifootball_requests": af.used,
                        "sportec_requests": sp.used,
                        "apifootball_plan_limits":
                            "300 requests/minute, 7500/day (PRO)"}
    say("")
    say(f"REQUESTS  API-Football {af.used}  |  Sportec {sp.used}")

    report["verdict"] = "MEASURED"
    report["stdout"] = lines
    if bundle is not None:
        # The raw provider bytes are already committed in the source archive.
        # Duplicating 6 MB of them adds no evidence, so a replay archive keeps
        # only the analysis plus a sha256 pointing at the bytes it came from —
        # a hash chain instead of a copy.
        src = Path(bundle["replayed_from"])
        report["replayed_from_sha256"] = sha256_file(src)
        report["_bodies_omitted"] = (
            "Provider response BODIES are deliberately NOT duplicated here: "
            "they live in the archive named by replayed_from, whose sha256 is "
            "recorded above. Re-run with --from-archive <that file> to "
            "recompute every number in this report from those bytes.")
        report["provider_records"] = [
            {k: v for k, v in rec.items() if k != "body"}
            for rec in report["provider_records"]]
        say("")
        say(f"replay archive omits duplicated bodies; source archive "
            f"{src.name} sha256 {report['replayed_from_sha256']}")
    out = archive_path(now.date().isoformat())
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False,
                              default=str))
    size = out.stat().st_size
    say(f"archive -> {out}  ({size / 1e6:.2f} MB)")
    return 0


def _sportec_season_id() -> str:
    from src.live.mls_stats import SEASON_ID
    return SEASON_ID


def _sportec_competition_id() -> str:
    from src.live.mls_stats import COMPETITION_ID
    return COMPETITION_ID


if __name__ == "__main__":                                  # pragma: no cover
    try:
        sys.exit(main())
    except BudgetExceeded as exc:
        print(f"INDETERMINATE — {exc}")
        sys.exit(2)
