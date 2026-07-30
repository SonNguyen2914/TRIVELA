"""epl-2026-v0 — the EPL model scaffold (2026-07-28). SHIPS DARK.

Follows model_mls.py's shape: an interpretable goals-rate baseline run
through the SHARED Monte Carlo engine, with every parameter to be
fitted from EPL 2026-27 data held in the live database. Two deliberate
differences from the MLS module:

  1. GOALS-ONLY. No trustworthy xG source exists for the EPL in this
     stack (research_archive/epl/RESEARCH_SUMMARY_2026-07-28.json: ESPN
     eng.1 carries zero xG fields; no public no-auth official stats API
     analogous to stats-api.mlssoccer.com is known; API-Football's free
     plan is season-blind and its xG unverified; scraping sources are
     rejected). There is no EPL analogue of MLS_XG_RATING_ALPHA and no
     xG anywhere in this module — the gap is documented, not papered
     over.

  2. DARK BY CONSTRUCTION. No approval decision exists for epl-2026,
     ensure_model_version is only ever called with
     approved_for_shadow=False, and no code path here or anywhere else
     can create an EPL approval. The F3/F9 gates in src/live/runs.py
     therefore refuse every EPL run and every EPL lock, and no EPL odds
     render on any surface. Approval must be EARNED through the
     evaluation ladder on real 2026-27 data, exactly as MLS earned its
     — which also means the shrinkage/dispersion/calibration constants
     below are STARTING POINTS carried from the closest measured
     precedent (MLS league play), not validated EPL values. They must
     be re-swept on EPL data before any approval evaluation is run.

Season context: 20 clubs, 38 rounds, first fixture 2026-08-21. Until
completed fixtures exist, current_model() returns None and every run
path reports "no model" — the honest state, never a placeholder number.
"""
from __future__ import annotations

import hashlib
import math
import os
from datetime import timezone

import config
from src.live.db import get_session, plane_ready
from src.live.models import Fixture, ModelVersion

MODEL_NAME = "epl-2026-v0"
COMPETITION_SLUG = "epl-2026"

# STARTING POINTS, not EPL-validated (see module docstring). SHRINK_GAMES
# and HALF_LIFE_DAYS were swept on MLS 2026 (162 fixtures); MIN_GAMES is
# the shared history floor. Re-sweep on EPL data before any approval.
SHRINK_GAMES = 24.0         # Bayesian prior weight (games at league avg)
HALF_LIFE_DAYS = 90.0       # recency half-life for rate weighting
MIN_GAMES = 5               # a team needs history before it's rated
RESULT_SHRINK = 8.0         # w/d/l display rates (never a probability)


def _weight(days_ago: float) -> float:
    return 0.5 ** (max(days_ago, 0.0) / HALF_LIFE_DAYS)


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


def fit(fixtures, as_of) -> dict | None:
    """Ratings + league parameters from completed fixtures. Pure
    function of its inputs (the walk-forward validator calls it with
    prior-only slices). GOALS-ONLY — no xG anywhere (see docstring)."""
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
        "elo": 1500.0,           # only the DIFFERENCE is consumed
        "league_base": model["league_gpg"],
        "venue_mult": model[f"venue_{venue}"],
    }


UNIFORM_3WAY = {"home_win": 1 / 3, "draw": 1 / 3, "away_win": 1 / 3}


def calibrate(outcomes: dict, alpha: float) -> dict:
    """Shrink the simulated 3-way toward uniform. EPL default alpha is
    0.0 (no EPL overconfidence has been MEASURED yet — the MLS 0.25 was
    measured on MLS data and does not transfer by assumption)."""
    if alpha <= 0:
        return outcomes
    a = min(alpha, 1.0)
    blended = {k: (1 - a) * outcomes.get(k, 0.0)
               + a * UNIFORM_3WAY.get(k, 0.0) for k in outcomes}
    s = sum(blended.values())
    return {k: v / s for k, v in blended.items()} if s > 0 else outcomes


def seed_for(fixture, run_type: str) -> int:
    """Deterministic per-(fixture, run_type) seed from STABLE provider
    identity, masked to 31 bits (signed int4 on PostgreSQL)."""
    ident = getattr(fixture, "espn_event_id", None) or str(fixture)
    h = hashlib.sha256(
        f"{MODEL_NAME}:{COMPETITION_SLUG}:espn:{ident}:{run_type}"
        .encode()).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


INPUT_ARTIFACT_SCHEMA = "model-input-epl-v1"
_GIT_REV = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:40]


def _canonical(doc: dict) -> str:
    import json as _json
    return _json.dumps(doc, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))


def engine_signature(code_revision: str | None = None) -> dict:
    """Fingerprint of the ACTUAL engine implementation + runtime an EPL
    replay depends on — same design as model_mls.engine_signature (V9.1
    eval F5), hashing THIS module's source in place of model_mls's."""
    import importlib
    import platform

    import numpy as _np

    from src.models.simulator import RED_CARD_OPP_MULT, RED_CARD_OWN_MULT
    from src.models.xg_model import MODEL_VERSION as _XG_VERSION
    from src.models.xg_model import SET_PIECE_BASELINE
    constants = {
        "set_piece_baseline": SET_PIECE_BASELINE,
        "goal_dispersion_cv": config.EPL_GOAL_DISPERSION_CV,
        "red_card_own_mult": RED_CARD_OWN_MULT,
        "red_card_opp_mult": RED_CARD_OPP_MULT,
        "red_card_risk_default": 0.06,
        "xg_model_version": _XG_VERSION,
    }
    source_sha256: dict[str, str | None] = {}
    for name in ("src.live.model_epl", "src.models.simulator",
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


def build_input_artifact(fixture, model: dict,
                         run_type: str) -> tuple[dict, str, str]:
    """The exact, RETRIEVABLE input document a run simulates from.
    Same contract as model_mls.build_input_artifact; no xG fields
    because no xG exists (schema model-input-epl-v1)."""
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
        },
        "league": {
            "league_gpg": model["league_gpg"],
            "venue_home": model["venue_home"],
            "venue_away": model["venue_away"],
            "n_fixtures": model["n_fixtures"],
        },
        "team_ratings": {"home": home_r, "away": away_r},
        "calibration": {
            "alpha": config.EPL_CALIBRATION_ALPHA,
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


def replay_from_artifact(document: dict,
                         n_sims: int | None = None) -> dict | None:
    """Deterministic replay from the stored input DOCUMENT alone."""
    from src.models.simulator import MatchSimulator
    from src.models.xg_model import SET_PIECE_BASELINE
    tr = document.get("team_ratings") or {}
    lg = document.get("league") or {}
    sim_cfg = document.get("simulation") or {}
    if not tr.get("home") or not tr.get("away"):
        return None
    eng = (document.get("engine") or {}).get("constants") or {}
    set_piece = eng.get("set_piece_baseline", SET_PIECE_BASELINE)
    red_risk = eng.get("red_card_risk_default", 0.06)

    def raw(r, venue):
        return {
            "attack": r["attack"], "defence": r["defence"],
            "form": 0.5, "fatigue": 0.0,
            "set_piece_threat": set_piece,
            "red_card_risk": red_risk, "elo": 1500.0,
            "league_base": lg["league_gpg"],
            "venue_mult": lg[f"venue_{venue}"],
        }

    sim = MatchSimulator(
        n_simulations=n_sims or sim_cfg.get("draws"),
        seed=sim_cfg.get("seed"),
        dispersion_cv=eng.get("goal_dispersion_cv"))
    out = sim.simulate(raw(tr["home"], "home"),
                       raw(tr["away"], "away"), stage="group")
    cal = document.get("calibration") or {}
    return calibrate(out["outcomes"], cal.get("alpha", 0.0))


def predict_fixture(fixture, model: dict, run_type: str = "scheduled",
                    n_sims: int | None = None) -> dict | None:
    """One fixture's shadow prediction via the shared engine. Runs only
    in tests/backtests until approval exists — the runs.py gates refuse
    everything else."""
    home = _raw(fixture.home_team_id, model, "home")
    away = _raw(fixture.away_team_id, model, "away")
    if home is None or away is None:
        return None
    from src.models.simulator import MatchSimulator
    sim = MatchSimulator(n_simulations=n_sims,
                         seed=seed_for(fixture, run_type),
                         dispersion_cv=config.EPL_GOAL_DISPERSION_CV)
    out = sim.simulate(home, away, stage="group")
    outcomes = calibrate(out["outcomes"], config.EPL_CALIBRATION_ALPHA)
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
        "xg": out["xg"],       # the SIMULATOR's expected goals — model
                               # output, NOT provider xG (none exists)
        "basis": {
            "home_games": model["ratings"][fixture.home_team_id]["games"],
            "away_games": model["ratings"][fixture.away_team_id]["games"],
            "league_gpg": round(model["league_gpg"], 3),
            "venue_home": round(model["venue_home"], 3),
            "home_attack": round(model["ratings"][fixture.home_team_id]["attack"], 3),
            "home_defence": round(model["ratings"][fixture.home_team_id]["defence"], 3),
            "away_attack": round(model["ratings"][fixture.away_team_id]["attack"], 3),
            "away_defence": round(model["ratings"][fixture.away_team_id]["defence"], 3),
        },
    }


def current_model() -> dict | None:
    """Fit from everything completed as of now. None until the 2026-27
    season produces completed fixtures — the honest empty."""
    if not plane_ready():
        return None
    from datetime import datetime
    s = get_session()
    try:
        rows = _completed(s)
    finally:
        s.close()
    return fit(rows, datetime.now(timezone.utc))


# --- rolling-origin validation (the ladder substrate, pre-approval) --------

def _logloss3(p: dict, result: str) -> float:
    q = max(min(p[result], 1 - 1e-6), 1e-6)
    return -math.log(q)


def backtest(n_sims: int = 4000) -> dict:
    """Walk-forward over the season vs the flat-ratings baseline —
    exactly model_mls.backtest's design. Empty (n=0) until completed
    2026-27 fixtures exist; approval evaluation builds on this."""
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
    """Upsert the model_version row. The EPL boot only ever calls this
    with approved_for_shadow=False — the model is DARK, and approval is
    an explicit operator act after an evaluation that does not exist
    yet. approved_for_real_money is NEVER set anywhere."""
    if not plane_ready():
        return
    from datetime import datetime
    s = get_session()
    try:
        row = s.query(ModelVersion).filter_by(name=MODEL_NAME).first()
        if row is None:
            row = ModelVersion(name=MODEL_NAME,
                               description="EPL goals-rate baseline "
                                           "scaffold (DARK: unapproved, "
                                           "no xG source exists)",
                               created_at=datetime.now(timezone.utc))
            s.add(row)
        row.approved_for_shadow = bool(approved_for_shadow)
        s.commit()
    finally:
        s.close()
