"""mls-2026-v0 — the first MLS model (launch decision O8).

An interpretable goals-rate baseline run through the SHARED Monte Carlo
engine, with every parameter fitted from MLS 2026 data held in the live
database — WC26 values are structure, never coefficients:

  - attack_i  = shrunk, recency-weighted (GF/game)_i relative to league
  - defence_i = shrunk, recency-weighted (GA/game)_i relative to league
  - league_base = the season's goals per team-game (fitted, not 1.30)
  - venue multipliers = fitted home/away scoring split (not WC-neutral)
  - set-piece term: NEUTRAL (threat == baseline -> centered adj == 0);
    per the decision, no set-piece adjustment without decomposed inputs
  - form neutral 0.5, fatigue 0 (no validated covariates yet — a zero
    is honest where data quality is weak)

Validation: rolling-origin walk-forward over the season's completed
fixtures (no future match ever informs an earlier rating), scored
against a league-average baseline. approved_for_shadow is EARNED by
beating that baseline, not assumed.
"""
from __future__ import annotations

import hashlib
import math
import os
from datetime import timezone

import config
from src.live.db import get_session, plane_ready
from src.live.models import Fixture, ModelVersion

MODEL_NAME = "mls-2026-v0"
# SHRINK_GAMES chosen by the rolling-origin sweep of Jul 23 (n=162
# fixtures, 4000 sims): k=6 LOST to the flat baseline (-0.007 logloss);
# k in [20,56] is uniformly positive with a stable optimum near 24
# (+0.007). MLS scoring rates are noisy enough that a season's raw
# GF/GA needs to be pulled hard toward the mean. The edge is real but
# SMALL — one more reason the money gate stays closed.
SHRINK_GAMES = 24.0         # Bayesian prior weight (games at league avg)
HALF_LIFE_DAYS = 90.0       # recency half-life for rate weighting
MIN_GAMES = 5               # a team needs history before it's rated
# win% (results) signal: shrink w/d/l rates toward a flat prior so
# small-sample teams aren't over-weighted, and BLEND the results-based
# 3-way into the simulated 3-way by this weight. alpha is measured on the
# NOTE: these win/draw/loss rates NO LONGER affect any probability. The
# Jul 24 audit showed the blend that consumed them carried no team
# information; it was replaced by an explicit calibration term. They are
# kept because they are honest, displayable form data.
RESULT_SHRINK = 8.0
# xG ratings get their OWN, much lighter shrinkage. k=24 above was swept
# for GOALS, which are noisy enough that a season's raw GF/GA must be
# pulled hard toward the mean (k=6 LOST to baseline there). Provider xG is
# a far lower-variance per-match estimate, so the same prior weight throws
# away most of the signal: with ~7.4 recency-weighted games it left a team
# only 24% of its own rating, flattening the league's best xG side (VAN,
# +1.52 xGD/game) to near-average and letting home advantage outrank it.
# Re-swept Jul 24 on the same 162-match walk-forward with xG ratings on:
# monotonic gain from k=24 down to a clean INTERIOR optimum at k=4-6
# (+0.0076 log loss, turning back up below k=3). k=6 takes essentially all
# of it while staying the more conservative side of the optimum.
XG_SHRINK_GAMES = float(os.getenv("MLS_XG_SHRINK_GAMES", "6.0"))


def _weight(days_ago: float) -> float:
    return 0.5 ** (max(days_ago, 0.0) / HALF_LIFE_DAYS)


def _completed(s, before=None):
    q = (s.query(Fixture)
         .filter_by(competition_slug="mls-2026", status="post")
         .filter(Fixture.home_goals.isnot(None),
                 Fixture.home_team_id.isnot(None),
                 Fixture.away_team_id.isnot(None)))
    rows = [f for f in q.all() if f.current_kickoff_utc is not None]
    if before is not None:
        rows = [f for f in rows if _utc(f.current_kickoff_utc) < before]
    rows.sort(key=lambda f: _utc(f.current_kickoff_utc))
    return rows


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fit(fixtures, as_of, xg_by_fixture: dict | None = None,
        xg_alpha: float = 0.0,
        xg_shrink: float | None = None) -> dict | None:
    """Ratings + league parameters from a list of completed fixtures.
    Pure function of its inputs — the walk-forward validator calls it
    with prior-only slices.

    xG-BASED RATINGS (xg_alpha>0): when a per-fixture xG map is supplied
    (from mls_stats.team_xg_map, keyed by fixture id), each team's
    attack/defence is blended toward a rating built on the provider's
    expected goals instead of actual goals. xG is the less-noisy signal
    over a half-season. A fixture missing xG contributes only to the goals
    rating for that team, so partial coverage degrades gracefully and
    xg_alpha=0 is byte-identical to the pure-goals model."""
    if not fixtures:
        return None
    gf: dict[int, float] = {}
    ga: dict[int, float] = {}
    w_sum: dict[int, float] = {}
    n_games: dict[int, int] = {}
    # xG-for / xG-against accumulators + their own weight sum (only over
    # matches that carry provider xG) — a separately-shrunk rating
    xgf: dict[int, float] = {}
    xga: dict[int, float] = {}
    xw_sum: dict[int, float] = {}
    # recency-weighted results (win/draw/loss) per team — the win% signal
    # (captures teams whose RESULTS outperform their goal difference, which
    # a pure goals-Poisson model under-rates)
    wins: dict[int, float] = {}
    draws: dict[int, float] = {}
    losses: dict[int, float] = {}
    tot_home = tot_away = tot_w = 0.0
    tot_xg = txg_w = 0.0
    xgm = xg_by_fixture or {}
    for f in fixtures:
        days = (as_of - _utc(f.current_kickoff_utc)).total_seconds() / 86400
        w = _weight(days)
        if f.home_goals > f.away_goals:
            h_res, a_res = "w", "l"
        elif f.home_goals < f.away_goals:
            h_res, a_res = "l", "w"
        else:
            h_res = a_res = "d"
        fxg = xgm.get(getattr(f, "id", None)) if xg_alpha > 0 else None
        for team, scored, conceded, res, sidek in (
                (f.home_team_id, f.home_goals, f.away_goals, h_res, "home"),
                (f.away_team_id, f.away_goals, f.home_goals, a_res, "away")):
            gf[team] = gf.get(team, 0.0) + w * scored
            ga[team] = ga.get(team, 0.0) + w * conceded
            w_sum[team] = w_sum.get(team, 0.0) + w
            n_games[team] = n_games.get(team, 0) + 1
            bucket = {"w": wins, "d": draws, "l": losses}[res]
            bucket[team] = bucket.get(team, 0.0) + w
            side = fxg.get(sidek) if fxg else None
            if side and side.get("xg") is not None \
                    and side.get("xg_against") is not None:
                xgf[team] = xgf.get(team, 0.0) + w * side["xg"]
                xga[team] = xga.get(team, 0.0) + w * side["xg_against"]
                xw_sum[team] = xw_sum.get(team, 0.0) + w
                tot_xg += w * side["xg"]
                txg_w += w
        tot_home += w * f.home_goals
        tot_away += w * f.away_goals
        tot_w += w
    if tot_w <= 0:
        return None
    league_gpg = (tot_home + tot_away) / (2 * tot_w)
    if league_gpg <= 0:
        return None
    # league average xG per team-game (the denominator for xG ratings);
    # falls back to the goal scale if no xG was seen
    league_xg = (tot_xg / txg_w) if txg_w > 0 else league_gpg
    ratings = {}
    results = {}
    for team, w in w_sum.items():
        # shrink toward league average by prior weight (in games)
        k = SHRINK_GAMES
        atk = (gf[team] / league_gpg + k) / (w + k)
        dfc = (ga[team] / league_gpg + k) / (w + k)
        # blend in an xG-based rating (each shrunk on its own xG sample,
        # with the LIGHTER xG prior — see XG_SHRINK_GAMES)
        xw = xw_sum.get(team, 0.0)
        if xg_alpha > 0 and xw > 0 and league_xg > 0:
            kx = xg_shrink if xg_shrink is not None else XG_SHRINK_GAMES
            atk_x = (xgf[team] / league_xg + kx) / (xw + kx)
            dfc_x = (xga[team] / league_xg + kx) / (xw + kx)
            atk = (1 - xg_alpha) * atk + xg_alpha * atk_x
            dfc = (1 - xg_alpha) * dfc + xg_alpha * dfc_x
        ratings[team] = {"attack": atk, "defence": dfc,
                         "games": n_games[team]}
        # win/draw/loss RATES, shrunk toward the league draw-ish prior so a
        # team with few games isn't over-weighted (same discipline as the
        # goals ratings — small samples get pulled to the mean)
        kr = RESULT_SHRINK
        results[team] = {
            "w": (wins.get(team, 0.0) + kr / 3) / (w + kr),
            "d": (draws.get(team, 0.0) + kr / 3) / (w + kr),
            "l": (losses.get(team, 0.0) + kr / 3) / (w + kr),
            "games": n_games[team]}
    return {
        "results": results,
        "league_gpg": league_gpg,
        "league_xg": league_xg,
        "xg_alpha": xg_alpha,
        "xg_shrink": (xg_shrink if xg_shrink is not None
                      else XG_SHRINK_GAMES),
        # fraction of team-match observations (2 per fixture) carrying xG
        "xg_coverage": round(txg_w / (2 * tot_w), 3) if tot_w else 0.0,
        # fitted venue split: home teams score tot_home/tot_w per game
        "venue_home": (tot_home / tot_w) / league_gpg,
        "venue_away": (tot_away / tot_w) / league_gpg,
        "ratings": ratings,
        "n_fixtures": len(fixtures),
        # provenance for the retrievable input artifact: exactly which
        # completed fixtures (by stable provider id) fed this fit, and
        # the cutoff — so a fit can be reconstructed independently
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
        # engine pass-through; only elo DIFFERENCE is ever consumed
        # (WC26 anchoring), so equal values are a true neutral
        "elo": 1500.0,
        "league_base": model["league_gpg"],
        "venue_mult": model[f"venue_{venue}"],
    }


def results_prior(model: dict, home_id: int, away_id: int) -> dict | None:
    """A 3-way outcome prior from the two teams' recency-weighted win/draw/
    loss RATES (the win% signal). The home team's own win-rate maps to a
    home win; the away team's own win-rate maps to an away win; the two
    teams' views are averaged and normalized. Blended into the simulated
    3-way (never replacing it) so 'a team that wins a lot' nudges the
    probabilities without discarding the goals model."""
    res = model.get("results") or {}
    h, a = res.get(home_id), res.get(away_id)
    if not h or not a:
        return None
    p_home = (h["w"] + a["l"]) / 2      # H wins  /  A loses
    p_draw = (h["d"] + a["d"]) / 2
    p_away = (h["l"] + a["w"]) / 2      # H loses /  A wins
    tot = p_home + p_draw + p_away
    if tot <= 0:
        return None
    return {"home_win": p_home / tot, "draw": p_draw / tot,
            "away_win": p_away / tot}


UNIFORM_3WAY = {"home_win": 1 / 3, "draw": 1 / 3, "away_win": 1 / 3}


def calibrate(outcomes: dict, alpha: float) -> dict:
    """Shrink the simulated 3-way toward the uniform anchor:
    final = (1-alpha)*simulated + alpha*(1/3, 1/3, 1/3).

    The model is measurably OVERCONFIDENT — raw probabilities are too
    extreme — and this corrects it. It replaced a "win% blend" that
    pulled toward teams' win/draw/loss rates: the Jul 24 audit showed a
    flat anchor scored strictly better at the same weight (1.0445 vs
    1.0469), so that term carried no team information and was doing
    exactly this, under a name that claimed more.

    A no-op at alpha<=0. Never touches props or scorelines — only the
    3-way, which is what was measured."""
    if alpha <= 0:
        return outcomes
    a = min(alpha, 1.0)
    blended = {k: (1 - a) * outcomes.get(k, 0.0)
               + a * UNIFORM_3WAY.get(k, 0.0) for k in outcomes}
    s = sum(blended.values())
    return {k: v / s for k, v in blended.items()} if s > 0 else outcomes


def blend_with_results(outcomes: dict, prior: dict | None,
                       alpha: float) -> dict:
    """final = (1-alpha)*simulated + alpha*results_prior, renormalized. A
    no-op when alpha<=0 or the prior is unavailable (a team without enough
    history)."""
    if not prior or alpha <= 0:
        return outcomes
    blended = {k: (1 - alpha) * outcomes.get(k, 0.0)
               + alpha * prior.get(k, 0.0) for k in outcomes}
    s = sum(blended.values())
    return {k: v / s for k, v in blended.items()} if s > 0 else outcomes


def seed_for(fixture, run_type: str) -> int:
    """Deterministic per-(fixture, run_type) seed from STABLE identity:
    the provider event id, never the auto-increment row id (V8
    evaluation F10 — a database rebuild changed row ids and with them
    every 'deterministic' seed). Masked to 31 bits because
    prediction_run.simulation_seed is SIGNED 32-bit on PostgreSQL (an
    unmasked sha prefix killed every boot sweep on prod Jul 23)."""
    ident = getattr(fixture, "espn_event_id", None) or str(fixture)
    h = hashlib.sha256(
        f"{MODEL_NAME}:mls-2026:espn:{ident}:{run_type}"
        .encode()).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


# v3 (V9.1 eval F5): the engine signature now fingerprints the actual
# SOURCE of the model/simulator modules plus the runtime (code revision,
# python, numpy) — not just selected constants — so an implementation
# change changes the signature. v2 froze constants+numpy only (a code
# change could pass the guard); v1 froze no engine at all.
INPUT_ARTIFACT_SCHEMA = "model-input-v5"
_GIT_REV = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:40]


def _canonical(doc: dict) -> str:
    """Deterministic serialization for hashing + storage: sorted keys,
    compact separators, full-precision float round-trip, no machine
    paths. The same inputs on any machine produce the same bytes."""
    import json as _json
    return _json.dumps(doc, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))


def engine_signature() -> dict:
    """A fingerprint of the ACTUAL engine implementation + runtime a replay
    depends on (V9.1 eval F5). The V8 artifact froze no engine; v2 froze
    selected constants + numpy, so a logic change that left those constants
    alone could still pass the replay guard. This hashes the SOURCE of the
    model/simulator modules themselves, plus the code revision, python, and
    numpy versions — so an implementation change changes signature_hash and
    replay refuses. (Container/wheel digests are the stronger form; a
    source-tree digest is the portable version available in-process.)"""
    import importlib
    import platform

    import numpy as _np

    import config
    from src.models.simulator import RED_CARD_OPP_MULT, RED_CARD_OWN_MULT
    from src.models.xg_model import MODEL_VERSION as _XG_VERSION
    from src.models.xg_model import SET_PIECE_BASELINE
    constants = {
        "set_piece_baseline": SET_PIECE_BASELINE,
        # the dispersion THIS model actually simulates under — MLS uses its
        # own measured value, not the WC26 default. Recording the wrong one
        # would both mis-fingerprint the engine and make replay_from_artifact
        # (which reads this key) reconstruct a different simulator.
        "goal_dispersion_cv": config.MLS_GOAL_DISPERSION_CV,
        "red_card_own_mult": RED_CARD_OWN_MULT,
        "red_card_opp_mult": RED_CARD_OPP_MULT,
        "red_card_risk_default": 0.06,
        "xg_model_version": _XG_VERSION,
    }
    # source digest over the modules that actually compute the numbers
    source_sha256: dict[str, str | None] = {}
    for name in ("src.live.model_mls", "src.models.simulator",
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
        "code_revision": _GIT_REV,
    }
    fingerprint = {"constants": constants, "source_sha256": source_sha256,
                   "runtime": runtime}
    sig_hash = hashlib.sha256(_canonical(fingerprint).encode()).hexdigest()
    return {
        "constants": constants,
        "source_sha256": source_sha256,
        "runtime": runtime,
        "numpy": _np.__version__,            # kept for display/back-compat
        "python": platform.python_version(),
        "code_revision": _GIT_REV,
        "signature_hash": sig_hash,
    }


def build_input_artifact(fixture, model: dict,
                         run_type: str) -> tuple[dict, str, str]:
    """The exact, RETRIEVABLE input document a run simulates from
    (V8.1 evaluation Phase 2). Contains everything needed to replay the
    run independently: model constants, fitted league params, BOTH
    teams' ratings, the seed, the draw count, the source-fixture ids,
    and the cutoff. Returns (document, canonical_bytes, content_hash)."""
    import config
    home_r = model["ratings"].get(fixture.home_team_id)
    away_r = model["ratings"].get(fixture.away_team_id)
    doc = {
        "schema_version": INPUT_ARTIFACT_SCHEMA,
        "model": MODEL_NAME,
        "code_revision": _GIT_REV,
        # the frozen engine the run simulated under (V9 eval F4)
        "engine": engine_signature(),
        "fixture": {
            "provider": "espn",
            "event_id": str(getattr(fixture, "espn_event_id", "")),
            "competition": "mls-2026",
        },
        "data_cutoff": model.get("as_of"),
        "model_parameters": {
            "shrink_games": SHRINK_GAMES,
            "half_life_days": HALF_LIFE_DAYS,
            "min_games": MIN_GAMES,
            # xG-rating provenance (ratings already bake the blend in, so
            # replay from team_ratings is unaffected — these are audit)
            "xg_rating_alpha": model.get("xg_alpha", 0.0),
            "xg_shrink_games": model.get("xg_shrink"),
            "xg_coverage": model.get("xg_coverage", 0.0),
        },
        "league": {
            "league_gpg": model["league_gpg"],
            "venue_home": model["venue_home"],
            "venue_away": model["venue_away"],
            "n_fixtures": model["n_fixtures"],
        },
        "team_ratings": {"home": home_r, "away": away_r},
        # the CALIBRATION applied to the 3-way, frozen so a replay
        # reproduces the published probabilities, not just the raw
        # simulation (v5 replaced the v4 "win_blend" term)
        "calibration": {
            "alpha": config.MLS_CALIBRATION_ALPHA,
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
    """Back-compat: the content hash of the retrievable input artifact."""
    return build_input_artifact(fixture, model, run_type)[2]


def replay_from_artifact(document: dict,
                         n_sims: int | None = None) -> dict | None:
    """Deterministic replay (V8.1 evaluation Phase 2 acceptance test):
    from the stored input DOCUMENT alone — no live database — rebuild
    the two teams' engine features and re-run the simulation with the
    frozen seed. Same inputs + same seed => same probabilities."""
    from src.models.simulator import MatchSimulator
    from src.models.xg_model import SET_PIECE_BASELINE
    tr = document.get("team_ratings") or {}
    lg = document.get("league") or {}
    sim_cfg = document.get("simulation") or {}
    if not tr.get("home") or not tr.get("away"):
        return None
    # replay the injectable engine constants FROM the artifact when it
    # froze them (v2+); legacy v1 artifacts fall back to current (V9 F4)
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
    # reproduce the PUBLISHED 3-way from whatever post-processing the
    # artifact froze. v5 = calibration toward uniform; v4 = the old win%
    # blend, replayed from ITS frozen prior so historical locks still
    # reproduce byte-exactly; v1-v3 = the raw simulation.
    cal = document.get("calibration")
    if cal:
        return calibrate(out["outcomes"], cal.get("alpha", 0.0))
    wb = document.get("win_blend") or {}
    return blend_with_results(out["outcomes"], wb.get("prior"),
                              wb.get("alpha", 0.0))


def predict_fixture(fixture, model: dict, run_type: str = "scheduled",
                    n_sims: int | None = None) -> dict | None:
    """One fixture's shadow prediction via the shared engine."""
    home = _raw(fixture.home_team_id, model, "home")
    away = _raw(fixture.away_team_id, model, "away")
    if home is None or away is None:
        return None
    from src.models.simulator import MatchSimulator
    sim = MatchSimulator(n_simulations=n_sims,
                         seed=seed_for(fixture, run_type),
                         dispersion_cv=config.MLS_GOAL_DISPERSION_CV)
    out = sim.simulate(home, away, stage="group")
    # CALIBRATE the 3-way (never the props/scorelines): the simulation is
    # overconfident, so shrink it toward uniform by the measured weight
    outcomes = calibrate(out["outcomes"], config.MLS_CALIBRATION_ALPHA)
    # every probability a listed Kalshi family can consume: the totals
    # ladder, BTTS, margins (their "spread"), first team to score, and
    # team totals — ALL taken from the simulator's full-array marginals
    # (V8 evaluation F4: deriving team totals from the truncated
    # scoreline display list understated them by ~2pp systematically)
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
        "sim_outcomes": out["outcomes"],   # pre-calibration, for transparency
        "props": props,
        "scorelines": out["scorelines"][:12],
        "xg": out["xg"],
        "basis": {
            "home_games": model["ratings"][fixture.home_team_id]["games"],
            "away_games": model["ratings"][fixture.away_team_id]["games"],
            "league_gpg": round(model["league_gpg"], 3),
            "venue_home": round(model["venue_home"], 3),
            # fitted ratings relative to league 1.0 — the honest "how
            # they play" numbers (attack >1 scores more than average,
            # defence <1 concedes less)
            "home_attack": round(model["ratings"][fixture.home_team_id]["attack"], 3),
            "home_defence": round(model["ratings"][fixture.home_team_id]["defence"], 3),
            "away_attack": round(model["ratings"][fixture.away_team_id]["attack"], 3),
            "away_defence": round(model["ratings"][fixture.away_team_id]["defence"], 3),
        },
    }


def current_model() -> dict | None:
    """Fit from everything completed as of now. Uses xG-based ratings when
    config.MLS_XG_RATING_ALPHA>0 and the ingestion has populated team xG."""
    if not plane_ready():
        return None
    from datetime import datetime
    s = get_session()
    try:
        rows = _completed(s)
    finally:
        s.close()
    xg_map = None
    alpha = config.MLS_XG_RATING_ALPHA
    if alpha > 0:
        from src.live import mls_stats
        xg_map = mls_stats.team_xg_map()
    return fit(rows, datetime.now(timezone.utc),
               xg_by_fixture=xg_map, xg_alpha=alpha)


# --- rolling-origin validation --------------------------------------------

def _logloss3(p: dict, result: str) -> float:
    q = max(min(p[result], 1 - 1e-6), 1e-6)
    return -math.log(q)


def backtest(n_sims: int = 4000) -> dict:
    """Walk-forward over the season: every completed fixture whose two
    teams each have >= MIN_GAMES PRIOR completed games is predicted from
    prior-only data and scored against the result. Baseline = the same
    machinery with flat ratings (league average + fitted venue split) —
    v0 must beat what 'every team is identical' already knows."""
    if not plane_ready():
        return {"error": "dormant"}
    s = get_session()
    try:
        rows = _completed(s)
    finally:
        s.close()
    scored = []
    from src.models.simulator import MatchSimulator
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
            "brier_model": sum((o[k] - (1.0 if k == result else 0.0)) ** 2
                               for k in ("home_win", "draw", "away_win")),
            "brier_base": sum((b[k] - (1.0 if k == result else 0.0)) ** 2
                              for k in ("home_win", "draw", "away_win")),
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
        "logloss_edge": round(ll_b - ll_m, 4),      # positive = model wins
        "brier_model": round(sum(r["brier_model"] for r in scored) / n, 4),
        "brier_baseline": round(sum(r["brier_base"] for r in scored) / n, 4),
        "winner_hit_rate": round(sum(r["picked"] for r in scored) / n, 4),
        "beats_baseline": ll_m < ll_b,
    }


def ensure_model_version(approved_for_shadow: bool) -> None:
    """Upsert the model_version row with its earned approval flag.
    approved_for_real_money is NEVER set here — that flag belongs to the
    evidence-review gate alone."""
    if not plane_ready():
        return
    from datetime import datetime
    s = get_session()
    try:
        row = s.query(ModelVersion).filter_by(name=MODEL_NAME).first()
        if row is None:
            row = ModelVersion(name=MODEL_NAME,
                               description="goals-rate baseline, fitted "
                                           "MLS params, shared engine",
                               created_at=datetime.now(timezone.utc))
            s.add(row)
        row.approved_for_shadow = bool(approved_for_shadow)
        s.commit()
    finally:
        s.close()
