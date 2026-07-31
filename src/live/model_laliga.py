"""laliga-2026-v0 — the La Liga model scaffold. SHIPS DARK.

Shape mirrors model_mls.py: an interpretable goals-rate baseline run
through the SHARED Monte Carlo engine, fitted from la-liga-2026
fixtures in the live database. Three things distinguish it from the
MLS model, all deliberate:

  1. DARK. No ModelApprovalDecision exists for laliga-2026-v0, boot is
     fail-closed on approval, and nothing in this branch creates one —
     so scheduled_runs/t10_locks refuse at the per-model gates and no
     odds render anywhere. That is correct and required: a model earns
     approval through the evaluation ladder the way MLS did (rolling-
     origin walk-forward, bootstrap CIs, an explicit operator
     activation), and surfaces show the honest no-prediction empty
     state until then.

  2. NO xG. MLS's xG ratings ride an official public Sportec feed. No
     equivalent exists for La Liga (researched 2026-07-28, archived in
     research_archive/laliga_provider_research_2026-07-28.json: ESPN
     esp.1 carries no xG; the official apim.laliga.com is
     subscription-gated; Understat/FBref are contract-less scraping;
     API-Football free-plan visibility is unverified). Ratings are
     therefore goals-only — the MLS model's xg_alpha=0 path — and the
     gap is documented, never approximated.

  3. UNVALIDATED PARAMETERS. The shrinkage/recency constants are
     inherited from the MLS sweep as PLACEHOLDERS (League differences —
     more draws, lower scoring, a heavier head — are real). They MUST
     be re-swept on La Liga data before any approval; the missing
     approval decision is what enforces that ordering.

Only the PURE post-processing helpers (calibrate / blend / replay /
_utc) are imported from model_mls — importing never modifies that
file. The fit itself is a goals-only COPY with this module's own
constants, NOT a parameterization of model_mls.fit: model_mls.py is
inside the MLS engine signature (source hash), so touching it would
invalidate the live MLS approval and halt shadow collection — the
exact sequencing hazard AGENTS.md §12 defers to Son. The duplication
is the price of that isolation, and it is deliberate.
"""
from __future__ import annotations

import hashlib
import math
import os
from datetime import timezone

import config
from src.live.db import get_session, plane_ready
from src.live.model_mls import _utc, calibrate
from src.live.models import Fixture, ModelVersion

MODEL_NAME = "laliga-2026-v0"
COMPETITION_SLUG = "la-liga-2026"

# UNVALIDATED placeholders — inherited from the MLS sweeps, NOT swept on
# La Liga data. Kept as module constants (not silently shared) so the
# eventual La Liga sweep changes THIS file, and with it the engine
# signature, visibly.
SHRINK_GAMES = 24.0
HALF_LIFE_DAYS = 90.0
MIN_GAMES = 5
# Calibration defaults to ZERO: the MLS 0.25 was MEASURED on MLS
# residuals, and applying it here unmeasured would be a fabricated
# correction. The scaffold publishes the raw simulated 3-way until a
# La Liga audit earns a value.
LALIGA_CALIBRATION_ALPHA = float(
    os.getenv("LALIGA_CALIBRATION_ALPHA", "0.0"))
# Dispersion likewise unvalidated; 0.0 mirrors the MLS-measured value
# only as a starting point and must be re-measured here.
LALIGA_GOAL_DISPERSION_CV = float(
    os.getenv("LALIGA_GOAL_DISPERSION_CV", "0.0"))


# Results-rate shrink for the displayable form data (parity with the
# MLS surface; feeds no probability — the MLS audit showed the win%
# blend carried no information and it was retired there too).
RESULT_SHRINK = 8.0


def _weight(days_ago: float) -> float:
    return 0.5 ** (max(days_ago, 0.0) / HALF_LIFE_DAYS)


def fit(fixtures, as_of) -> dict | None:
    """Ratings + league parameters from completed fixtures — a
    GOALS-ONLY copy of model_mls.fit under this module's constants
    (see the module docstring for why it is a copy, not a call). Pure
    function of its inputs; the walk-forward validator feeds it
    prior-only slices. No xG accumulators exist here at all: absence
    of a trustworthy La Liga xG source is structural, not a config
    default."""
    if not fixtures:
        return None
    gf: dict[int, float] = {}
    ga: dict[int, float] = {}
    w_sum: dict[int, float] = {}
    n_games: dict[int, int] = {}
    wins: dict[int, float] = {}
    draws: dict[int, float] = {}
    losses: dict[int, float] = {}
    tot_home = tot_away = tot_w = 0.0
    for f in fixtures:
        days = (as_of - _utc(f.current_kickoff_utc)).total_seconds() / 86400
        w = _weight(days)
        if f.home_goals > f.away_goals:
            h_res, a_res = "w", "l"
        elif f.home_goals < f.away_goals:
            h_res, a_res = "l", "w"
        else:
            h_res = a_res = "d"
        for team, scored, conceded, res in (
                (f.home_team_id, f.home_goals, f.away_goals, h_res),
                (f.away_team_id, f.away_goals, f.home_goals, a_res)):
            gf[team] = gf.get(team, 0.0) + w * scored
            ga[team] = ga.get(team, 0.0) + w * conceded
            w_sum[team] = w_sum.get(team, 0.0) + w
            n_games[team] = n_games.get(team, 0) + 1
            bucket = {"w": wins, "d": draws, "l": losses}[res]
            bucket[team] = bucket.get(team, 0.0) + w
        tot_home += w * f.home_goals
        tot_away += w * f.away_goals
        tot_w += w
    if tot_w <= 0:
        return None
    league_gpg = (tot_home + tot_away) / (2 * tot_w)
    if league_gpg <= 0:
        return None
    ratings = {}
    results = {}
    for team, w in w_sum.items():
        k = SHRINK_GAMES
        ratings[team] = {
            "attack": (gf[team] / league_gpg + k) / (w + k),
            "defence": (ga[team] / league_gpg + k) / (w + k),
            "games": n_games[team]}
        kr = RESULT_SHRINK
        results[team] = {
            "w": (wins.get(team, 0.0) + kr / 3) / (w + kr),
            "d": (draws.get(team, 0.0) + kr / 3) / (w + kr),
            "l": (losses.get(team, 0.0) + kr / 3) / (w + kr),
            "games": n_games[team]}
    return {
        "results": results,
        "league_gpg": league_gpg,
        "venue_home": (tot_home / tot_w) / league_gpg,
        "venue_away": (tot_away / tot_w) / league_gpg,
        "ratings": ratings,
        "n_fixtures": len(fixtures),
        "source_fixtures": sorted(
            str(f.espn_event_id) for f in fixtures
            if getattr(f, "espn_event_id", None)),
        "as_of": as_of.isoformat(),
    }


def _completed(s, before=None):
    q = (s.query(Fixture)
         .filter_by(competition_slug=COMPETITION_SLUG, status="post")
         .filter(Fixture.home_goals.isnot(None),
                 Fixture.home_team_id.isnot(None),
                 Fixture.away_team_id.isnot(None)))
    rows = [f for f in q.all() if f.current_kickoff_utc is not None]
    if before is not None:
        rows = [f for f in rows if _utc(f.current_kickoff_utc) < before]
    rows.sort(key=lambda f: _utc(f.current_kickoff_utc))
    return rows


def _raw(team_id: int, model: dict, venue: str) -> dict | None:
    r = model["ratings"].get(team_id)
    if r is None or r["games"] < MIN_GAMES:
        return None
    from src.models.xg_model import SET_PIECE_BASELINE
    return {
        "attack": r["attack"], "defence": r["defence"],
        "form": 0.5, "fatigue": 0.0,
        "set_piece_threat": SET_PIECE_BASELINE,   # centered adj == 0
        "red_card_risk": 0.06,
        "elo": 1500.0,
        "league_base": model["league_gpg"],
        "venue_mult": model[f"venue_{venue}"],
    }


def seed_for(fixture, run_type: str) -> int:
    """Deterministic per-(fixture, run_type) seed from STABLE identity
    (provider event id, never the row id), masked to 31 bits for the
    signed-32-bit PG column — both MLS lessons, kept."""
    ident = getattr(fixture, "espn_event_id", None) or str(fixture)
    h = hashlib.sha256(
        f"{MODEL_NAME}:{COMPETITION_SLUG}:espn:{ident}:{run_type}"
        .encode()).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


INPUT_ARTIFACT_SCHEMA = "model-input-v5"
_GIT_REV = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:40]


def _canonical(doc: dict) -> str:
    import json as _json
    return _json.dumps(doc, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))


def engine_signature(code_revision: str | None = None) -> dict:
    """Fingerprint of THIS league's engine: the model module source, the
    shared simulator/xg-model/features sources, constants, and runtime —
    same discipline as model_mls.engine_signature (V9.1 eval F5)."""
    import importlib
    import platform

    import numpy as _np

    from src.models.simulator import RED_CARD_OPP_MULT, RED_CARD_OWN_MULT
    from src.models.xg_model import MODEL_VERSION as _XG_VERSION
    from src.models.xg_model import SET_PIECE_BASELINE
    constants = {
        "set_piece_baseline": SET_PIECE_BASELINE,
        "goal_dispersion_cv": LALIGA_GOAL_DISPERSION_CV,
        "red_card_own_mult": RED_CARD_OWN_MULT,
        "red_card_opp_mult": RED_CARD_OPP_MULT,
        "red_card_risk_default": 0.06,
        "xg_model_version": _XG_VERSION,
        "calibration_alpha": LALIGA_CALIBRATION_ALPHA,
    }
    source_sha256: dict[str, str | None] = {}
    for name in ("src.live.model_laliga", "src.models.simulator",
                 "src.models.xg_model", "src.models.features"):
        try:
            mod = importlib.import_module(name)
            with open(mod.__file__, "rb") as fh:
                source_sha256[name] = hashlib.sha256(fh.read()).hexdigest()
        except (OSError, ImportError, AttributeError, TypeError):
            source_sha256[name] = None
    runtime = {
        "python": platform.python_version(),
        "numpy": _np.__version__,
        "code_revision": code_revision or _GIT_REV,
    }
    fingerprint = {"constants": constants, "source_sha256": source_sha256,
                   "runtime": runtime}
    sig_hash = hashlib.sha256(_canonical(fingerprint).encode()).hexdigest()
    return {
        "constants": constants,
        "source_sha256": source_sha256,
        "runtime": runtime,
        "numpy": _np.__version__,
        "python": platform.python_version(),
        "code_revision": runtime["code_revision"],
        "signature_hash": sig_hash,
    }


def engine_matches(stored_hash: str | None,
                   run_revision: str | None) -> tuple[bool, bool]:
    """(matches, revision_only_drift) for a stored La Liga engine
    signature — the same contract as model_mls/model_epl.engine_matches
    (V9.1 eval F4).

    Required by the competition registry, not optional: since the EPL
    build, `src/live/audit.py` and `verify_replay` resolve the model
    module through `competitions.model_module(slug)` and call this on
    whatever they get. Registering la-liga-2026 without it would make
    every registry-routed audit of a La Liga run raise AttributeError
    instead of answering. (`replay_from_artifact`, the other half of
    that contract, is already re-exported from model_mls at the foot of
    this file — league-neutral because it reads the stored document.)

    matches=True when the stored hash equals the current engine's, OR
    when recomputing under the revision the run RECORDED reproduces it —
    which proves only the build revision moved. revision_only_drift flags
    that second case so the distinction stays visible. A genuine source,
    constant or runtime change fails both arms."""
    if not stored_hash:
        return False, False
    if stored_hash == engine_signature()["signature_hash"]:
        return True, False
    if run_revision:
        rehashed = engine_signature(code_revision=run_revision)
        if rehashed["signature_hash"] == stored_hash:
            return True, True
    return False, False


def build_input_artifact(fixture, model: dict,
                         run_type: str) -> tuple[dict, str, str]:
    """The exact, RETRIEVABLE input document a run simulates from —
    the model_mls artifact contract, keyed to this league."""
    home_r = model["ratings"].get(fixture.home_team_id)
    away_r = model["ratings"].get(fixture.away_team_id)
    doc = {
        "schema_version": INPUT_ARTIFACT_SCHEMA,
        "model": MODEL_NAME,
        "code_revision": _GIT_REV,
        "engine": engine_signature(),
        "fixture": {
            "provider": "espn",
            "event_id": str(getattr(fixture, "espn_event_id", "")),
            "competition": COMPETITION_SLUG,
        },
        "data_cutoff": model.get("as_of"),
        "model_parameters": {
            "shrink_games": SHRINK_GAMES,
            "half_life_days": HALF_LIFE_DAYS,
            "min_games": MIN_GAMES,
            # goals-only by construction: no trustworthy La Liga xG
            # source exists (researched 2026-07-28) — recorded here so
            # the artifact says so, not just the docs
            "xg_rating_alpha": 0.0,
            "xg_shrink_games": None,
            "xg_coverage": 0.0,
        },
        "league": {
            "league_gpg": model["league_gpg"],
            "venue_home": model["venue_home"],
            "venue_away": model["venue_away"],
            "n_fixtures": model["n_fixtures"],
        },
        "team_ratings": {"home": home_r, "away": away_r},
        "calibration": {
            "alpha": LALIGA_CALIBRATION_ALPHA,
            "anchor": "uniform_3way",
        },
        "simulation": {
            "seed": seed_for(fixture, run_type),
            "draws": config.N_SIMULATIONS,
            "run_type": run_type,
        },
        "source_fixtures": model.get("source_fixtures", []),
        "exclusions": [],
    }
    canon = _canonical(doc)
    return doc, canon, hashlib.sha256(canon.encode()).hexdigest()


def input_hash(fixture, model: dict, run_type: str = "scheduled") -> str:
    return build_input_artifact(fixture, model, run_type)[2]


def predict_fixture(fixture, model: dict, run_type: str = "scheduled",
                    n_sims: int | None = None) -> dict | None:
    """One fixture's shadow prediction via the shared engine. The same
    probability surface as MLS (3-way + the family props + scorelines),
    so the market machinery joins identically."""
    home = _raw(fixture.home_team_id, model, "home")
    away = _raw(fixture.away_team_id, model, "away")
    if home is None or away is None:
        return None
    from src.models.simulator import MatchSimulator
    sim = MatchSimulator(n_simulations=n_sims,
                         seed=seed_for(fixture, run_type),
                         dispersion_cv=LALIGA_GOAL_DISPERSION_CV)
    out = sim.simulate(home, away, stage="group")
    outcomes = calibrate(out["outcomes"], LALIGA_CALIBRATION_ALPHA)
    keep = ("btts", "over_0_5", "over_1_5", "over_2_5", "over_3_5",
            "over_4_5", "over_5_5", "home_margin_2", "home_margin_3",
            "away_margin_2", "away_margin_3", "home_first_goal",
            "away_first_goal", "no_goal",
            "home_team_over_0_5", "home_team_over_1_5",
            "home_team_over_2_5", "away_team_over_0_5",
            "away_team_over_1_5", "away_team_over_2_5")
    props = {k: out["props"][k] for k in keep if k in out["props"]}
    return {
        "model_version": MODEL_NAME,
        "seed": seed_for(fixture, run_type),
        "outcomes": outcomes,
        "sim_outcomes": out["outcomes"],
        "props": props,
        "scorelines": out["scorelines"][:12],
        "xg": out["xg"],
        "basis": {
            "home_games": model["ratings"][fixture.home_team_id]["games"],
            "away_games": model["ratings"][fixture.away_team_id]["games"],
            "league_gpg": round(model["league_gpg"], 3),
            "venue_home": round(model["venue_home"], 3),
            "home_attack": round(
                model["ratings"][fixture.home_team_id]["attack"], 3),
            "home_defence": round(
                model["ratings"][fixture.home_team_id]["defence"], 3),
            "away_attack": round(
                model["ratings"][fixture.away_team_id]["attack"], 3),
            "away_defence": round(
                model["ratings"][fixture.away_team_id]["defence"], 3),
        },
    }


def current_model() -> dict | None:
    """Fit from everything completed as of now. Goals-only — no xG map
    exists for this league (the documented gap). Returns None until the
    season produces completed fixtures, which keeps every run path in
    its honest 'no model' state through August."""
    if not plane_ready():
        return None
    from datetime import datetime
    s = get_session()
    try:
        rows = _completed(s)
    finally:
        s.close()
    return fit(rows, datetime.now(timezone.utc))


def _logloss3(p: dict, result: str) -> float:
    q = max(min(p[result], 1 - 1e-6), 1e-6)
    return -math.log(q)


def backtest(n_sims: int = 4000) -> dict:
    """Walk-forward over the season vs the flat-ratings baseline —
    the same discipline the MLS model earned its approval under. This
    is the substrate the future La Liga evaluation ladder scores; it
    proves nothing until the season provides fixtures."""
    if not plane_ready():
        return {"error": "dormant"}
    s = get_session()
    try:
        rows = _completed(s)
    finally:
        s.close()
    scored = []
    for i, f in enumerate(rows):
        prior = rows[:i]
        as_of = _utc(f.current_kickoff_utc)
        model = fit(prior, as_of)
        if model is None:
            continue
        pred = predict_fixture(f, model, run_type="backtest",
                               n_sims=n_sims)
        if pred is None:
            continue
        flat = dict(model)
        flat["ratings"] = {t: {"attack": 1.0, "defence": 1.0,
                               "games": model["ratings"][t]["games"]}
                           for t in model["ratings"]}
        base = predict_fixture(f, flat, run_type="baseline", n_sims=n_sims)
        result = ("home_win" if f.home_goals > f.away_goals else
                  "away_win" if f.away_goals > f.home_goals else "draw")
        o, b = pred["outcomes"], base["outcomes"]
        scored.append({
            "fixture": f.espn_event_id,
            "result": result,
            "model_p": o[result],
            "ll_model": _logloss3(o, result),
            "ll_base": _logloss3(b, result),
            "picked": max(o, key=o.get) == result,
        })
    n = len(scored)
    if n == 0:
        return {"n": 0, "error": "no scorable fixtures"}
    ll_m = sum(r["ll_model"] for r in scored) / n
    ll_b = sum(r["ll_base"] for r in scored) / n
    return {
        "model_version": MODEL_NAME, "n": n,
        "logloss_model": round(ll_m, 4),
        "logloss_baseline": round(ll_b, 4),
        "logloss_edge": round(ll_b - ll_m, 4),
        "winner_hit_rate": round(sum(r["picked"] for r in scored) / n, 4),
        "beats_baseline": ll_m < ll_b,
    }


def ensure_model_version(approved_for_shadow: bool = False) -> None:
    """Upsert the model_version row, and set the shadow flag to whatever
    the caller earned — defaulting to False, so every boot and every
    unqualified call leaves the model DARK.

    This signature is not cosmetic. It previously took no argument at
    all, while its three siblings (MLS, EPL, Liga MX) all take the flag,
    and `model_eval` calls every one of them as
    `ensure_model_version(approved_for_shadow=...)`. So the La Liga
    branch raised TypeError the moment an evaluation reached it — which
    only happened in production, where the plane has data; locally the
    ladder short-circuits on "dormant" first and never gets here. The
    operator activation route answered a bare 500 for it.

    The second defect was quieter: the flag was assigned INSIDE the
    `row is None` branch, so an existing row's approval could never be
    changed in either direction. A revocation would have reported
    success and left the model approved.

    approved_for_real_money is still never set here, by any path.
    """
    if not plane_ready():
        return
    from datetime import datetime
    s = get_session()
    try:
        row = s.query(ModelVersion).filter_by(name=MODEL_NAME).first()
        if row is None:
            row = ModelVersion(name=MODEL_NAME,
                               description="goals-rate scaffold, DARK — "
                                           "no approval; no La Liga xG "
                                           "source exists",
                               created_at=datetime.now(timezone.utc))
            s.add(row)
        row.approved_for_shadow = bool(approved_for_shadow)
        s.commit()
    finally:
        s.close()


# re-exported so the artifact-driven replay path (league-neutral by
# construction — it reads everything from the stored document) is
# reachable under this module too
from src.live.model_mls import replay_from_artifact  # noqa: E402,F401
