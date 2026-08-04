"""Model-development ladder + honest evaluation (V8.1 eval Phase 6).

The V8 evaluation raised two specific problems with the ad-hoc backtest:
  1. the model and its baseline used DIFFERENT simulation seeds, so
     their difference included independent Monte Carlo noise —
     "simulation noise masquerading as model improvement";
  2. the +0.007 log-loss edge had NO uncertainty estimate.

Both are fixed here. The 3-way outcome is scored with ANALYTIC
probabilities (independent-Poisson goal grid → exact P(home/draw/away)),
so there is zero simulation noise and every variant is compared on
identical ground. Uncertainty is a MATCH-CLUSTER bootstrap: resample
fixtures with replacement, recompute each variant's mean and every
pairwise edge, and report a 95% interval — so "M2 beats M0" is a claim
with a confidence interval, not a point estimate.

The ladder (evaluable rungs; M3+ await the inputs they need):
  M0  league scoring + home/away venue split (no team info)
  M1  team attack/defence ratings, equal-weighted, minimal pooling
  M2  + recency weighting + partial pooling  == mls-2026-v0
  M3  + rest / travel / surface           (pending covariates)
  M4  + availability / lineup effects      (data captured, not yet used)
  M5  + goalkeeper effects                 (data captured, not yet used)

Rolling-origin throughout: a fixture is predicted only from fixtures
that kicked off before it — no leakage by construction.

COMPETITION-KEYED since 2026-07-29 (backlog S-5). This module was
MLS-only: it imported MLS's hyperparameters at module scope and read
MLS's config constants inline, so no other league could be run up the
ladder. Every function now takes an optional `LadderSpec` and DEFAULTS TO
MLS, exactly as `runs.py` and `ingest.py` were generalized — so existing
MLS call sites and their numbers are unchanged, and that identity is
proven by a control (`tests/test_ladder_parity.py`) that runs the
pre-refactor module out of the git object store beside this one and
requires byte-identical reports.

`src/live/model_mls.py` is deliberately NOT modified by that work: it is
inside the MLS engine signature, so any source change there invalidates
the live MLS approval. The MLS spec READS it.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

import numpy as np

from src.live.db import get_session, plane_ready
from src.live.model_mls import HALF_LIFE_DAYS, MIN_GAMES, MODEL_NAME

EVAL_VERSION = "model-eval-v1"
APPROVAL_POLICY_VERSION = "shadow-approval-v1"
# A ladder run over PRIOR-SEASON history is a strictly weaker instrument
# than MLS's live-plane ladder, and gets its own policy name so no record
# can present the two as the same decision. See `replay_approval_policy`.
REPLAY_APPROVAL_POLICY_VERSION = "shadow-approval-replay-v1"
MIN_SCORED_FOR_APPROVAL = 30
GOAL_GRID = 15
THREE = ("home_win", "draw", "away_win")

# the evaluable rungs: (use_ratings, recency, shrink)
LADDER = {
    "M0": {"use_ratings": False, "recency": False, "shrink": 0.0,
           "desc": "league scoring + venue split"},
    "M1": {"use_ratings": True, "recency": False, "shrink": 1.0,
           "desc": "team ratings, equal-weighted, minimal pooling"},
    "M2": {"use_ratings": True, "recency": True, "shrink": 24.0,
           "desc": "+ recency + partial pooling (mls-2026-v0)"},
    # M2C replaced the old "M2W" win%-blend rung: the Jul 24 audit showed
    # that term carried no team information (a flat anchor beat the real
    # win/draw/loss prior at the same weight), so what it actually did —
    # correcting overconfidence — is now named and measured as such.
    "M2C": {"use_ratings": True, "recency": True, "shrink": 24.0,
            "calibrate": True,
            "desc": "+ calibration (shrink the 3-way toward uniform)"},
    "M3": {"use_ratings": True, "recency": True, "shrink": 24.0,
           "calibrate": True, "xg_from_config": True,
           "desc": "+ provider xG attack/defence ratings (own lighter "
                   "shrink — xG is far less noisy than goals)"},
}
FUTURE_RUNGS = {
    "M4": "availability / lineup effects — player stats captured "
          "(mls_stats), consumed once measured to help",
    "M5": "goalkeeper effects — player GK stats captured, "
          "consumed once measured to help",
}

# The pairwise edges MLS reports, in MLS's order. Frozen here rather than
# inline in evaluate_ladder so a league with fewer rungs can declare its
# own set without perturbing this one.
MLS_EDGE_PAIRS = (("M2", "M0"), ("M2", "M1"), ("M1", "M0"),
                  ("M2C", "M2"), ("M2C", "M0"),
                  ("M3", "M2C"), ("M3", "M0"))

# A goals-only ladder: no xG rung, because for these leagues no
# trustworthy team-level xG source exists in this stack (see
# model_epl.py / model_ligamx.py docstrings). An M3 rung here would claim
# a measurement that cannot be made.
GOALS_ONLY_LADDER = {
    "M0": {"use_ratings": False, "recency": False, "shrink": 0.0,
           "desc": "league scoring + venue split"},
    "M1": {"use_ratings": True, "recency": False, "shrink": 1.0,
           "desc": "team ratings, equal-weighted, minimal pooling"},
    "M2": {"use_ratings": True, "recency": True, "shrink": 24.0,
           "desc": "+ recency + partial pooling"},
    "M2C": {"use_ratings": True, "recency": True, "shrink": 24.0,
            "calibrate": True,
            "desc": "+ calibration (shrink the 3-way toward uniform)"},
}
GOALS_ONLY_EDGE_PAIRS = (("M2", "M0"), ("M2", "M1"), ("M1", "M0"),
                         ("M2C", "M2"), ("M2C", "M0"))
GOALS_ONLY_FUTURE_RUNGS = {
    # CORRECTED 2026-07-29: this used to say no trustworthy team-level xG
    # source EXISTS for these leagues. One does now — a paid API-Football key,
    # with per-league coverage MEASURED rather than assumed (see
    # src/live/apifootball.py). What the rung still waits on is fixture
    # OVERLAP: the provider's xG history is the completed previous season while
    # our live plane holds the current campaign, so the bridge is empty until
    # this season generates completed fixtures. "Unavailable" would now be
    # false; "measured, and waiting for overlap" is the truth, and
    # apifootball.bridge_coverage reports which.
    "M3": "provider xG ratings — a MEASURED xG source now exists for this "
          "league (API-Football), but the bridge to our own fixtures is "
          "empty until the current season generates completed fixtures; "
          "apifootball.bridge_coverage reports the measured overlap",
    "M4": "availability / lineup effects — no lineup capture yet",
    "M5": "goalkeeper effects — no GK capture yet",
}

# The goals-only ladder PLUS the provider-xG rung, for a league that has both a
# measured xG source and real overlap with our fixtures. Selected at call time
# by LadderSpec.active_ladder() — never at import, which would make the ladder
# a function of database state at module load.
LEAGUE_XG_LADDER = {
    **GOALS_ONLY_LADDER,
    "M3": {"use_ratings": True, "recency": True, "shrink": 24.0,
           "calibrate": True, "xg_alpha": 1.0,
           "desc": "+ provider xG attack/defence ratings (API-Football, "
                   "own lighter shrink — xG is far less noisy than goals)"},
}
LEAGUE_XG_EDGE_PAIRS = GOALS_ONLY_EDGE_PAIRS + (("M3", "M2C"), ("M3", "M0"))

# (competition_slug -> API-Football league id) for the leagues whose xG this
# stack can source. MLS is ABSENT deliberately: it keeps Sportec, and
# apifootball refuses league 253 by name.
DARK_LEAGUE_APIFOOTBALL_IDS = {
    "epl-2026": 39,
    "la-liga-2026": 140,
    "liga-mx-2026": 262,
}


class LadderSpec:
    """What one competition needs to be run up the ladder.

    Hyperparameters are READ from the league's own model module at call
    time (never copied here) so a spec cannot silently drift from the
    model it claims to evaluate — the MLS spec in particular must reflect
    `model_mls` exactly, since that module may not be edited.
    """

    def __init__(self, slug: str, model_module, ladder: dict,
                 edge_pairs, future_rungs: dict,
                 calibration_alpha_fn=None, xg_map_fn=None,
                 corpus_version_fn=None, model_parameters_fn=None,
                 label: str = "", xg_ladder: dict | None = None,
                 xg_edge_pairs=None):
        self.slug = slug
        self.model = model_module
        self.ladder = ladder
        self.edge_pairs = tuple(edge_pairs)
        self.future_rungs = future_rungs
        self.label = label or slug
        # config is read at call time, so an env flip needs no re-import
        self._calibration_alpha_fn = calibration_alpha_fn
        # None == this league has no provider xG (not "we didn't wire it")
        self.xg_map_fn = xg_map_fn
        self._corpus_version_fn = corpus_version_fn
        self._model_parameters_fn = model_parameters_fn
        # An OPTIONAL richer ladder, used only when this league's xG map is
        # actually populated. MLS passes neither and is unaffected.
        self.xg_ladder = xg_ladder
        self.xg_edge_pairs = tuple(xg_edge_pairs or ())

    # --- hyperparameters, read from the model module -------------------
    @property
    def model_name(self) -> str:
        return self.model.MODEL_NAME

    def half_life_days(self) -> float:
        return self.model.HALF_LIFE_DAYS

    def min_games(self) -> int:
        return self.model.MIN_GAMES

    def result_shrink(self) -> float:
        return self.model.RESULT_SHRINK

    def xg_shrink_default(self) -> float:
        return float(getattr(self.model, "XG_SHRINK_GAMES", 0.0))

    def calibration_alpha(self) -> float:
        if self._calibration_alpha_fn is None:
            return 0.0
        return float(self._calibration_alpha_fn())

    def calibrate(self, three: dict, alpha: float) -> dict:
        return self.model.calibrate(three, alpha)

    def completed(self, s, before=None):
        return self.model._completed(s, before=before)

    def xg_map(self) -> dict:
        return self.xg_map_fn() if self.xg_map_fn else {}

    def active_ladder(self) -> tuple[dict, tuple]:
        """(ladder, edge_pairs) for THIS run.

        MLS is returned untouched — its ladder and edge pairs are frozen, its
        approval rests on them, and `tests/test_ladder_parity.py` requires the
        report to stay byte-identical.

        For a league carrying `xg_ladder`, the xG rung is included only when the
        xG map is NON-EMPTY. That condition is the honest one: declaring an M3
        rung whose map is empty would report a rung measured on nothing, while
        omitting it when real xG is bridged would hide a measurement we can
        actually make. The map is read once here, not per rung."""
        if not self.xg_ladder or not self.xg_map_fn:
            return self.ladder, self.edge_pairs
        try:
            if self.xg_map():
                return self.xg_ladder, self.xg_edge_pairs
        except Exception as exc:                 # never fail a ladder run on
            print(f"[model_eval] xg map probe failed for "  # a provider read
                  f"{self.slug}: {exc}")
        return self.ladder, self.edge_pairs

    def latest_corpus_version(self) -> str | None:
        """The published corpus this league's approval can bind to. Only
        MLS publishes one; for every other league this is honestly None
        rather than borrowing MLS's."""
        return (self._corpus_version_fn() if self._corpus_version_fn
                else None)

    def model_parameters(self) -> dict:
        if self._model_parameters_fn is not None:
            return self._model_parameters_fn()
        return {
            "model_version": self.model_name,
            "engine_signature":
                self.model.engine_signature()["signature_hash"],
            "artifact_schema": getattr(
                self.model, "INPUT_ARTIFACT_SCHEMA", None),
            "parameters": {
                "goals_shrink_games": getattr(
                    self.model, "SHRINK_GAMES", None),
                "half_life_days": self.half_life_days(),
                "min_games": self.min_games(),
                "result_shrink": self.result_shrink(),
                "calibration_alpha": self.calibration_alpha(),
            },
            "parameter_provenance": (
                "STARTING POINTS carried from MLS league play, NOT swept "
                "on this league's own data unless the decision document "
                "says otherwise"),
        }


def _mls_calibration_alpha() -> float:
    import config
    return config.MLS_CALIBRATION_ALPHA


def _mls_xg_map() -> dict:
    from src.live import mls_stats
    return mls_stats.team_xg_map()


def _mls_corpus_version() -> str | None:
    from src.live import corpus as _c
    return _c.latest_published_version()


def _mls_model_parameters() -> dict:
    from src.live import corpus as _c
    return _c._model_parameters()


def _mls_spec() -> LadderSpec:
    from src.live import model_mls
    return LadderSpec(
        slug="mls-2026", model_module=model_mls, ladder=LADDER,
        edge_pairs=MLS_EDGE_PAIRS, future_rungs=FUTURE_RUNGS,
        calibration_alpha_fn=_mls_calibration_alpha,
        xg_map_fn=_mls_xg_map,
        corpus_version_fn=_mls_corpus_version,
        model_parameters_fn=_mls_model_parameters,
        label="MLS")


MLS_LADDER_SPEC = _mls_spec()

# The registry. A new league joins by ADDING A ROW — nothing else. That is
# the load-bearing property for La Liga, which lives on `feat-la-liga`
# (not yet rebased onto the EPL/Liga MX stack): when its module lands, one
# entry here gives it the ladder, the replay and the approval path.
#
# (module path, config-constant prefix, alert label)
_DARK_LEAGUE_REGISTRY = (
    ("epl-2026", "src.live.model_epl", "EPL_CALIBRATION_ALPHA", "EPL"),
    ("liga-mx-2026", "src.live.model_ligamx", "LIGAMX_CALIBRATION_ALPHA",
     "LIGAMX"),
    ("la-liga-2026", "src.live.model_laliga", "LALIGA_CALIBRATION_ALPHA",
     "LALIGA"),
    # A row here is a declaration, not a promise: a module that will not
    # import is skipped, not fatal, so this file stays importable on a
    # branch where a league has not landed yet.
)


def _apifootball_xg_map_fn(slug: str, league_id: int):
    """A per-competition xG map reader, in the same shape MLS's Sportec reader
    has. Returns {} when nothing bridges — which `active_ladder` treats as 'no
    M3 rung', never as a rung measured on zeros."""
    def _fn() -> dict:
        from src.live import apifootball
        return apifootball.bridge_fixture_xg(slug, league_id)
    return _fn


def _config_alpha_fn(name: str):
    def _fn() -> float:
        import config
        return float(getattr(config, name, 0.0) or 0.0)
    return _fn


def _build_specs() -> dict:
    import importlib
    specs = {MLS_LADDER_SPEC.slug: MLS_LADDER_SPEC}
    for slug, module_path, alpha_const, label in _DARK_LEAGUE_REGISTRY:
        try:
            mod = importlib.import_module(module_path)
        except ImportError:
            # the league's model module is not on this branch yet — the
            # registry row is a declaration, not a promise
            continue
        apif_id = DARK_LEAGUE_APIFOOTBALL_IDS.get(slug)
        specs[slug] = LadderSpec(
            slug=slug, model_module=mod, ladder=GOALS_ONLY_LADDER,
            edge_pairs=GOALS_ONLY_EDGE_PAIRS,
            future_rungs=GOALS_ONLY_FUTURE_RUNGS,
            calibration_alpha_fn=_config_alpha_fn(alpha_const),
            # a MEASURED xG source, bridged onto our own fixtures. None where
            # this stack has no id for the league — honestly absent, not wired
            # to an empty stub.
            xg_map_fn=(_apifootball_xg_map_fn(slug, apif_id)
                       if apif_id else None),
            xg_ladder=(LEAGUE_XG_LADDER if apif_id else None),
            xg_edge_pairs=(LEAGUE_XG_EDGE_PAIRS if apif_id else None),
            label=label)
    return specs


_SPECS: dict | None = None


def ladder_specs() -> dict:
    """{slug: LadderSpec} for every competition whose model module is
    importable on this branch."""
    global _SPECS
    if _SPECS is None:
        _SPECS = _build_specs()
    return _SPECS


def ladder_spec(slug: str | None = None) -> LadderSpec:
    """Resolve a spec by slug; None means MLS (the default that keeps
    every pre-existing call site meaning what it meant)."""
    if slug is None:
        return MLS_LADDER_SPEC
    spec = ladder_specs().get(slug)
    if spec is None:
        raise KeyError(f"no ladder spec for competition {slug!r} "
                       f"(known: {sorted(ladder_specs())})")
    return spec


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pois_pmf(lam: float) -> np.ndarray:
    lam = max(lam, 1e-6)
    k = np.arange(GOAL_GRID + 1)
    logf = np.array([math.lgamma(i + 1) for i in k])
    return np.exp(-lam + k * math.log(lam) - logf)


def analytic_3way(lam_h: float, lam_a: float) -> dict:
    """Exact P(home/draw/away) from independent Poisson goal counts —
    no simulation, hence no Monte Carlo noise."""
    ph, pa = _pois_pmf(lam_h), _pois_pmf(lam_a)
    joint = np.outer(ph, pa)                 # joint[h, a]
    home = float(np.tril(joint, -1).sum())   # h > a
    draw = float(np.trace(joint))            # h == a
    away = float(np.triu(joint, 1).sum())    # h < a
    tot = home + draw + away
    return {"home_win": home / tot, "draw": draw / tot,
            "away_win": away / tot}


def fit_variant(fixtures, as_of, cfg: dict,
                xg_by_fixture: dict | None = None,
                spec: LadderSpec | None = None) -> dict | None:
    """Ratings + league params under a ladder config. Pure function of
    its inputs; the walk-forward calls it with prior-only slices. Mirrors
    the league model's fit — including the xG-based rating blend
    (cfg['xg_alpha']) so the M3 rung measures exactly the deployed
    feature. `spec` defaults to MLS."""
    if not fixtures:
        return None
    spec = spec or MLS_LADDER_SPEC
    half_life = spec.half_life_days()
    RESULT_SHRINK = spec.result_shrink()
    # xG weight: an explicit cfg value (offline sweeps) wins; otherwise the
    # M3 rung tracks the deployed config so the ladder measures what ships
    xg_alpha = cfg.get("xg_alpha")
    if xg_alpha is None and cfg.get("xg_from_config"):
        import config as _cfg
        xg_alpha = _cfg.MLS_XG_RATING_ALPHA
    xg_alpha = float(xg_alpha or 0.0)
    # xG carries its OWN, lighter prior than goals (see XG_SHRINK_GAMES);
    # a sweep may override it explicitly
    xg_shrink = float(cfg.get("xg_shrink", spec.xg_shrink_default()))
    gf, ga, wsum, games = {}, {}, {}, {}
    xgf, xga, xwsum = {}, {}, {}
    wins, draws, losses = {}, {}, {}
    tot_home = tot_away = tot_w = 0.0
    tot_xg = txg_w = 0.0
    xgm = xg_by_fixture or {}
    for f in fixtures:
        if cfg["recency"]:
            days = (as_of - _utc(f.current_kickoff_utc)).total_seconds() / 86400
            w = 0.5 ** (max(days, 0.0) / half_life)
        else:
            w = 1.0
        if f.home_goals > f.away_goals:
            hr, ar = "w", "l"
        elif f.home_goals < f.away_goals:
            hr, ar = "l", "w"
        else:
            hr = ar = "d"
        fxg = xgm.get(getattr(f, "id", None)) if xg_alpha > 0 else None
        for team, sc, co, r, sidek in (
                (f.home_team_id, f.home_goals, f.away_goals, hr, "home"),
                (f.away_team_id, f.away_goals, f.home_goals, ar, "away")):
            gf[team] = gf.get(team, 0.0) + w * sc
            ga[team] = ga.get(team, 0.0) + w * co
            wsum[team] = wsum.get(team, 0.0) + w
            games[team] = games.get(team, 0) + 1
            {"w": wins, "d": draws, "l": losses}[r][team] = \
                {"w": wins, "d": draws, "l": losses}[r].get(team, 0.0) + w
            side = fxg.get(sidek) if fxg else None
            if side and side.get("xg") is not None \
                    and side.get("xg_against") is not None:
                xgf[team] = xgf.get(team, 0.0) + w * side["xg"]
                xga[team] = xga.get(team, 0.0) + w * side["xg_against"]
                xwsum[team] = xwsum.get(team, 0.0) + w
                tot_xg += w * side["xg"]
                txg_w += w
        tot_home += w * f.home_goals
        tot_away += w * f.away_goals
        tot_w += w
    if tot_w <= 0:
        return None
    league = (tot_home + tot_away) / (2 * tot_w)
    if league <= 0:
        return None
    league_xg = (tot_xg / txg_w) if txg_w > 0 else league
    ratings = {}
    results = {}
    if cfg["use_ratings"]:
        k = cfg["shrink"]
        for team, w in wsum.items():
            atk = (gf[team] / league + k) / (w + k)
            dfc = (ga[team] / league + k) / (w + k)
            xw = xwsum.get(team, 0.0)
            if xg_alpha > 0 and xw > 0 and league_xg > 0:
                kx = xg_shrink
                atk_x = (xgf[team] / league_xg + kx) / (xw + kx)
                dfc_x = (xga[team] / league_xg + kx) / (xw + kx)
                atk = (1 - xg_alpha) * atk + xg_alpha * atk_x
                dfc = (1 - xg_alpha) * dfc + xg_alpha * dfc_x
            ratings[team] = {"attack": atk, "defence": dfc,
                             "games": games[team]}
            kr = RESULT_SHRINK
            results[team] = {
                "w": (wins.get(team, 0.0) + kr / 3) / (w + kr),
                "d": (draws.get(team, 0.0) + kr / 3) / (w + kr),
                "l": (losses.get(team, 0.0) + kr / 3) / (w + kr)}
    return {"league": league,
            "venue_home": (tot_home / tot_w) / league,
            "venue_away": (tot_away / tot_w) / league,
            "ratings": ratings, "results": results,
            "use_ratings": cfg["use_ratings"],
            "calibrate": cfg.get("calibrate", False)}


def predict_variant(model: dict, fixture,
                    spec: LadderSpec | None = None) -> dict | None:
    """Analytic 3-way for one fixture under a fitted variant. `spec`
    defaults to MLS."""
    spec = spec or MLS_LADDER_SPEC
    min_games = spec.min_games()
    lh_v, la_v = model["venue_home"], model["venue_away"]
    if model["use_ratings"]:
        h = model["ratings"].get(fixture.home_team_id)
        a = model["ratings"].get(fixture.away_team_id)
        if h is None or a is None or h["games"] < min_games \
                or a["games"] < min_games:
            return None
        lam_h = model["league"] * h["attack"] * a["defence"] * lh_v
        lam_a = model["league"] * a["attack"] * h["defence"] * la_v
    else:
        # M0 still needs enough history to be a fair comparison point
        lam_h = model["league"] * lh_v
        lam_a = model["league"] * la_v
    three = analytic_3way(lam_h, lam_a)
    if model.get("calibrate"):
        three = spec.calibrate(three, spec.calibration_alpha())
    return three


def _rps(p: dict, result: str) -> float:
    """Ranked probability score for the ordered outcome home>draw>away."""
    order = ["home_win", "draw", "away_win"]
    cp = co = 0.0
    s = 0.0
    for k in order[:-1]:
        cp += p[k]
        co += 1.0 if k == result else 0.0
        s += (cp - co) ** 2
    return s / (len(order) - 1)


def _score_fixture(p: dict, result: str) -> dict:
    q = max(min(p[result], 1 - 1e-9), 1e-9)
    return {
        "log_loss": -math.log(q),
        "brier": sum((p[k] - (1.0 if k == result else 0.0)) ** 2
                     for k in THREE),
        "rps": _rps(p, result),
    }


def score_rows(rows, spec: LadderSpec | None = None,
               xg_by_fixture: dict | None = None,
               prior_fn=None) -> tuple[list, list[dict]]:
    """Rolling-origin per-fixture scores for every rung.

    Returns `(keys, per_fixture)` — parallel lists, so a caller can PAIR
    two different fitting policies on exactly the fixtures both could
    predict. That is what the Liga MX split-season question needs: it
    compares "ratings carry across the Apertura→Clausura boundary" with
    "ratings reset at it", and an unpaired comparison would confound the
    policy with a different (larger) scored sample.

    `prior_fn(rows, i)` supplies the history slice used to predict
    `rows[i]`; the default is `rows[:i]`, the plain rolling origin. It
    exists so a segment-resetting policy can restrict history without a
    second copy of this loop.
    """
    spec = spec or MLS_LADDER_SPEC
    ladder, _pairs = spec.active_ladder()
    xg_map = xg_by_fixture or {}
    prior_fn = prior_fn or (lambda rs, i: rs[:i])
    keys: list = []
    per_fixture: list[dict] = []
    for i, f in enumerate(rows):
        prior, as_of = prior_fn(rows, i), _utc(f.current_kickoff_utc)
        preds = {}
        ok = True
        for name, cfg in ladder.items():
            m = fit_variant(prior, as_of, cfg, xg_by_fixture=xg_map,
                            spec=spec)
            p = predict_variant(m, f, spec=spec) if m else None
            if p is None:
                ok = False
                break
            preds[name] = p
        if not ok:
            continue
        result = ("home_win" if f.home_goals > f.away_goals else
                  "away_win" if f.away_goals > f.home_goals else "draw")
        keys.append(getattr(f, "espn_event_id", None) or getattr(f, "id", i))
        per_fixture.append({name: _score_fixture(preds[name], result)
                            for name in ladder})
    return keys, per_fixture


def ladder_from_fixtures(rows, spec: LadderSpec | None = None,
                         n_boot: int = 1000, seed: int = 12345,
                         xg_by_fixture: dict | None = None,
                         prior_fn=None) -> dict:
    """The ladder itself, over an ORDERED list of completed fixtures.

    Extracted from `evaluate_ladder` so the same scoring, the same
    rolling origin and the same match-cluster bootstrap serve both the
    live-plane read and the prior-season replay — one instrument, two
    input sources, rather than a second implementation that could drift.

    `rows` must be sorted by kickoff ascending and carry
    current_kickoff_utc / home_team_id / away_team_id / home_goals /
    away_goals (plus `id` when an xG map is supplied).
    """
    spec = spec or MLS_LADDER_SPEC
    ladder, edge_pairs = spec.active_ladder()
    _keys, per_fixture = score_rows(rows, spec=spec,
                                    xg_by_fixture=xg_by_fixture,
                                    prior_fn=prior_fn)
    n = len(per_fixture)
    if n == 0:
        return {"n_scored": 0, "note": "no fixtures scorable by all rungs"}

    def mean(name, metric, idx):
        return float(np.mean([per_fixture[j][name][metric] for j in idx]))

    full = list(range(n))
    variants = {
        name: {"log_loss": round(mean(name, "log_loss", full), 4),
               "brier": round(mean(name, "brier", full), 4),
               "rps": round(mean(name, "rps", full), 4),
               "desc": ladder[name]["desc"]}
        for name in ladder}

    # match-cluster bootstrap: resample fixtures with replacement
    rng = np.random.default_rng(seed)
    pairs = [tuple(p) for p in edge_pairs]
    boot = {f"{a}_vs_{b}": [] for a, b in pairs}
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        for a, b in pairs:
            # positive edge = a has LOWER log loss than b (a is better)
            edge = mean(b, "log_loss", idx) - mean(a, "log_loss", idx)
            boot[f"{a}_vs_{b}"].append(edge)
    edges = {}
    for a, b in pairs:
        arr = np.array(boot[f"{a}_vs_{b}"])
        lo, hi = np.percentile(arr, [2.5, 97.5])
        point = mean(b, "log_loss", full) - mean(a, "log_loss", full)
        edges[f"{a}_vs_{b}"] = {
            "delta_log_loss": round(point, 4),
            "ci95": [round(float(lo), 4), round(float(hi), 4)],
            "significant": bool(lo > 0 or hi < 0),
        }
    return {
        "eval_version": EVAL_VERSION,
        "method": ("analytic independent-Poisson 3-way, rolling-origin, "
                   "match-cluster bootstrap (no Monte Carlo noise)"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_scored": n, "n_bootstrap": n_boot,
        "variants": variants, "edges": edges,
        "future_rungs": spec.future_rungs,
    }


def evaluate_ladder(n_boot: int = 1000, seed: int = 12345,
                    spec: LadderSpec | None = None) -> dict:
    """Rolling-origin evaluation of every evaluable rung with analytic
    scoring and match-cluster bootstrap CIs on the pairwise edges, over
    THIS competition's completed fixtures in the LIVE plane. `spec`
    defaults to MLS, so every pre-existing call site is unchanged."""
    if not plane_ready():
        return {"error": "dormant"}
    spec = spec or MLS_LADDER_SPEC
    s = get_session()
    try:
        rows = spec.completed(s)
    finally:
        s.close()
    # provider xG per fixture, loaded once (M3); rungs without xg_alpha
    # ignore it. Keyed by fixture id, so slicing `rows` restricts it too.
    # A league with no xG source supplies no map at all — which is the
    # honest state, not a zero-filled one.
    xg_map = spec.xg_map()
    return ladder_from_fixtures(rows, spec=spec, n_boot=n_boot, seed=seed,
                                xg_by_fixture=xg_map)


def evaluate_deployed(n_sims: int | None = None, seed: int = 12345) -> dict:
    """Score the EXACT deployed probability generator (V9.3 eval F9).

    The ladder above scores an analytic independent-Poisson representation
    so variant comparisons carry no Monte Carlo noise. Production is not
    that: it runs the shared simulator, which also samples red cards and
    applies their rate adjustments, and then calibrates. So the ladder
    validates the fitted mean-rate structure and the calibration step —
    NOT every component of what actually ships.

    This walks the same rolling origin but predicts through
    model_mls.predict_fixture, i.e. the production path with production
    seeds, and reports its metrics beside the analytic ones so the gap is
    a measured number rather than an assumption. It is Monte Carlo and
    therefore slow, so it is a diagnostic, not a boot-time step.

    IMPORTANT: the comparison is only meaningful at the PRODUCTION
    simulation count. Monte Carlo noise moves probabilities around and log
    loss punishes that, so a cheap run makes the deployed path look worse
    than it is, and a single cheap run is not even reproducible across
    seeds.

    Measured n=162, converging with sim count:
        1,200 sims   deployed 1.0453   (noise-dominated)
        4,000 sims   deployed 1.0453
       10,000 sims   deployed 1.0444   vs analytic 1.0443

    At the production count the difference is -0.0001 — the analytic
    ladder is a faithful proxy for the deployed generator, red-card
    sampling included. That is now a measured fact rather than an
    assumption, which is what the finding asked for."""
    if not plane_ready():
        return {"error": "dormant"}
    import config

    from src.live import mls_stats, model_mls
    n_sims = n_sims or config.N_SIMULATIONS
    s = get_session()
    try:
        rows = model_mls._completed(s)
    finally:
        s.close()
    xg = mls_stats.team_xg_map()
    alpha = config.MLS_XG_RATING_ALPHA
    an_ll, dep_ll, dep_brier = [], [], []
    for i, f in enumerate(rows):
        prior, as_of = rows[:i], _utc(f.current_kickoff_utc)
        m = model_mls.fit(prior, as_of, xg_by_fixture=xg, xg_alpha=alpha)
        if m is None:
            continue
        variant = fit_variant(prior, as_of, LADDER[deployed_variant()],
                              xg_by_fixture=xg)
        analytic = predict_variant(variant, f) if variant else None
        deployed = model_mls.predict_fixture(f, m, run_type="deployed-eval",
                                             n_sims=n_sims)
        if analytic is None or deployed is None:
            continue
        result = ("home_win" if f.home_goals > f.away_goals else
                  "away_win" if f.away_goals > f.home_goals else "draw")
        an_ll.append(_score_fixture(analytic, result)["log_loss"])
        sc = _score_fixture(deployed["outcomes"], result)
        dep_ll.append(sc["log_loss"])
        dep_brier.append(sc["brier"])
    n = len(dep_ll)
    if n == 0:
        return {"n_scored": 0, "note": "no scorable fixtures"}
    a, d = float(np.mean(an_ll)), float(np.mean(dep_ll))
    return {
        "n_scored": n, "n_simulations": n_sims,
        "analytic_log_loss": round(a, 4),
        "deployed_log_loss": round(d, 4),
        "deployed_brier": round(float(np.mean(dep_brier)), 4),
        "analytic_minus_deployed": round(a - d, 4),
        "reported_figure_is": ("conservative" if a > d else "optimistic"),
        "note": ("the ladder scores an analytic independent-Poisson "
                 "representation; production also samples red cards and "
                 "calibrates. This scores the production path itself."),
    }


def paired_edge(scores_a: list[dict], scores_b: list[dict], rung: str,
                n_boot: int = 1000, seed: int = 12345,
                metric: str = "log_loss") -> dict:
    """A match-cluster bootstrap edge between TWO POLICIES on the SAME
    fixtures — the paired counterpart of the pairwise rung edges.

    `scores_a` and `scores_b` must be parallel: element j of each is the
    same fixture scored under the two policies. Positive edge = policy A
    has the LOWER loss, i.e. A is better, matching the sign convention of
    the rung edges above.

    Used for the Liga MX split-season question, where the two policies
    differ only in whether ratings carry across the tournament boundary.
    Pairing is what makes that comparison legitimate: the two policies
    score DIFFERENT numbers of fixtures overall, so an unpaired interval
    would mix the policy effect with a changed sample.
    """
    n = len(scores_a)
    if n == 0 or n != len(scores_b):
        return {"n": n, "error": "unpaired or empty score lists"}
    a = np.array([s[rung][metric] for s in scores_a])
    b = np.array([s[rung][metric] for s in scores_b])
    diff = b - a
    rng = np.random.default_rng(seed)
    boot = np.array([float(np.mean(diff[rng.integers(0, n, n)]))
                     for _ in range(n_boot)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {
        "rung": rung, "metric": metric, "n_paired": n,
        f"{metric}_a": round(float(np.mean(a)), 4),
        f"{metric}_b": round(float(np.mean(b)), 4),
        "delta": round(float(np.mean(diff)), 4),
        "ci95": [round(float(lo), 4), round(float(hi), 4)],
        "significant": bool(lo > 0 or hi < 0),
        "n_bootstrap": n_boot,
    }


def replay_approval_policy(report: dict,
                           spec: LadderSpec | None = None
                           ) -> tuple[bool, str]:
    """The shadow-approval decision for a PRIOR-SEASON REPLAY ladder.

    Deliberately STRICTER than `shadow_approval_policy`, which governs
    MLS's live-plane decision. That policy approves a model whose edge CI
    spans zero, because shadow means "safe to collect prospective
    evidence" and MLS's ladder reads the same competition-season the
    approval licenses.

    A prior-season replay does not. It reads a DIFFERENT season — a
    different squad list, in two of these leagues a partly different set
    of clubs entirely — so it is a weaker instrument, and it gets a
    correspondingly higher bar: the M2 rung must actually BEAT its own M0
    baseline on a point estimate, not merely fail to lose significantly.

    A league whose M2 does not beat its M0 is REFUSED and stays dark.
    That is a correct outcome of the evaluation, not a failure of it.
    """
    spec = spec or MLS_LADDER_SPEC
    n = report.get("n_scored", 0)
    if n < MIN_SCORED_FOR_APPROVAL:
        return False, (f"insufficient scored sample (n={n} < "
                       f"{MIN_SCORED_FOR_APPROVAL})")
    e = (report.get("edges") or {}).get("M2_vs_M0") or {}
    point = e.get("delta_log_loss")
    if point is None:
        return False, "no M2-vs-M0 edge computed"
    if e.get("significant") and point < 0:
        return False, (f"M2 is SIGNIFICANTLY WORSE than its own M0 "
                       f"baseline on replay (edge {point}, "
                       f"CI {e.get('ci95')}) — model stays dark")
    if point <= 0:
        return False, (f"M2 does not beat its M0 baseline on replay "
                       f"(edge {point}, CI {e.get('ci95')}) — REPLAYED "
                       f"evidence is weaker than MLS's live-plane ladder, "
                       f"so a non-positive point estimate is refused "
                       f"rather than read as 'within noise'. Model stays "
                       f"dark.")
    return True, (
        f"M2 beats its own M0 baseline on REPLAYED prior-season history "
        f"(edge {point}, CI {e.get('ci95')}, "
        f"significant={e.get('significant')}) — safe to collect "
        f"prospective evidence, NOT an established edge, and NOT "
        f"comparable to a prospective result")


def deployed_variant(spec: LadderSpec | None = None) -> str:
    """The ladder rung that matches the DEPLOYED model: M3 when xG ratings
    are on, else M2C when calibration is on, else M2. The approval
    decision evaluates THIS variant so the persisted edge reflects what
    actually ships.

    `spec` defaults to MLS. For a league with no xG rung at all, M3 is
    not merely off — it is absent from the ladder, so it can never be
    selected here.

    The xG gate differs by league, and deliberately: MLS's rung tracks
    `MLS_XG_RATING_ALPHA` because that env flag is what ships in the MLS
    engine, while a league-derived rung carries its own `xg_alpha` in the rung
    config and is present in the ladder only when real xG actually bridged
    (see `active_ladder`). Reading MLS's flag for another league would gate one
    competition's rung on a different competition's deployment switch."""
    import config
    spec = spec or MLS_LADDER_SPEC
    ladder, _pairs = spec.active_ladder()
    if "M3" in ladder:
        if spec.slug == MLS_LADDER_SPEC.slug:
            if config.MLS_XG_RATING_ALPHA > 0:
                return "M3"
        elif float(ladder["M3"].get("xg_alpha") or 0.0) > 0:
            return "M3"
    if "M2C" in ladder and spec.calibration_alpha() > 0:
        return "M2C"
    return "M2"


def approval_record(report: dict, corpus_version: str | None = None,
                    spec: LadderSpec | None = None,
                    evidence: dict | None = None,
                    rung: str | None = None) -> dict:
    """The model-approval decision record (V8.1 eval Phase 6). Shadow
    approval means 'safe to collect prospective evidence', explicitly
    NOT 'edge established' — and this record never grants a higher mode.
    Evaluates the DEPLOYED variant (M3 when xG ratings are on).

    `evidence` carries the evidence-class block for a decision computed
    from something other than this competition's own live plane (the
    prior-season replay). It is folded into the limitations so the
    weakness travels with the decision rather than living in a report
    nobody reads next to it.
    """
    spec = spec or MLS_LADDER_SPEC
    # `rung` overrides the deployed variant. The replay path pins it to M2
    # because the prior-season archive carries no provider xG, so an M3
    # label there would claim a measurement that was never made.
    dv = rung or deployed_variant(spec)
    m2 = (report.get("variants") or {}).get(dv, {})
    e = (report.get("edges") or {}).get(f"{dv}_vs_M0", {})
    limitations = [
        "in-sample rolling-origin (not a prospective holdout)",
        "n and CI must be read together — a small point estimate with a "
        "CI spanning 0 is NOT an established edge",
        "M4-M5 rungs (lineup availability / GK) not yet implemented",
        # V9.3 eval F9 — say plainly what these metrics cover
        "metrics score an ANALYTIC independent-Poisson representation; the "
        "deployed simulator also samples red cards, so these validate the "
        "fitted mean-rate structure and calibration, not every component "
        "of the shipped generator (see evaluate_deployed)",
        # V9.3 eval F8 — and that selection used this same sample
        "hyperparameters (xG weight/shrink, calibration, dispersion) were "
        "swept on THIS evaluation sample, so the interval is conditional "
        "on the selected model and excludes model-selection uncertainty",
        "forecast quality only — market-relative and execution "
        "performance evaluated separately, after settlement",
    ]
    rec = {
        "model_version": spec.model_name,
        "corpus_version": corpus_version,
        "eval_version": report.get("eval_version"),
        "metrics": {"log_loss": m2.get("log_loss"),
                    "brier": m2.get("brier"), "rps": m2.get("rps"),
                    "n_scored": report.get("n_scored")},
        "edge_vs_baseline": e,
        "limitations": limitations,
        "approved_mode": "shadow",
        "approval_meaning": ("safe to collect prospective evidence; "
                             "NOT an established executable edge"),
        "approved_by": "automated-eval",
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    if evidence:
        # the evidence class leads the limitations, because it is the
        # single most load-bearing caveat on a replay decision
        rec["evidence"] = evidence
        rec["limitations"] = [
            f"EVIDENCE CLASS {evidence.get('evidence_class')}: "
            f"{evidence.get('evidence_note')}",
        ] + limitations
        rec["evaluated_rung"] = dv
    return rec


def shadow_approval_policy(report: dict) -> tuple[bool, str]:
    """The shadow-approval decision from the CONFIDENCE-INTERVAL evaluator
    (V9 eval F1) — never a bare Monte-Carlo point estimate. Shadow means
    'safe to collect prospective evidence', so it does NOT require a
    positive edge; but it REFUSES a model the evaluation shows is
    SIGNIFICANTLY worse than the league/venue baseline, and requires a
    minimum scored sample. A CI that spans zero is approvable for shadow
    (evidence collection), and the record says so — it is never 'edge
    established'."""
    n = report.get("n_scored", 0)
    if n < MIN_SCORED_FOR_APPROVAL:
        return False, f"insufficient scored sample (n={n} < " \
                      f"{MIN_SCORED_FOR_APPROVAL})"
    dv = deployed_variant()
    e = (report.get("edges") or {}).get(f"{dv}_vs_M0") or {}
    point = e.get("delta_log_loss")
    if point is None:
        return False, f"no {dv}-vs-baseline edge computed"
    if e.get("significant") and point < 0:
        return False, (f"model is SIGNIFICANTLY worse than baseline "
                       f"(edge {point}, CI {e.get('ci95')})")
    return True, ("edge within/above noise vs baseline — safe to collect "
                  "prospective evidence, NOT an established edge")


def _data_cutoff(spec: LadderSpec | None = None) -> str | None:
    """The kickoff of the most recent fixture that could inform this
    approval — the data boundary the decision was computed on."""
    spec = spec or MLS_LADDER_SPEC
    s = get_session()
    try:
        rows = spec.completed(s)
        return _utc(rows[-1].current_kickoff_utc).isoformat() if rows else None
    finally:
        s.close()


def _published_corpus_hash(version: str | None) -> str | None:
    """The manifest hash of the PUBLISHED corpus this approval is bound
    to, or None when no corpus has been published yet (V9.3 eval F7)."""
    if not version:
        return None
    from src.live.models import CorpusExport
    s = get_session()
    try:
        row = s.query(CorpusExport).filter_by(version=version).first()
        return row.manifest_hash if row else None
    finally:
        s.close()


def _decision_canonical(rec: dict) -> str:
    """The canonical bytes a decision's content_hash covers (V9.1 eval F4).
    Excludes wall-clock fields so an unchanged evaluation dedupes to one
    immutable row. Stored verbatim as `decision_document` so the audit can
    recompute and verify the hash independently."""
    from src.live.model_mls import _canonical
    core = {k: rec.get(k) for k in (
        "model_version", "eval_version", "policy_version", "corpus_version",
        "approved_mode", "approved", "metrics", "edge_vs_baseline",
        "decision_reason", "engine_signature",
        # V9.3 eval F7 — the decision hash now covers WHAT was approved
        # (exact parameters), on WHICH data (cutoff) and against which
        # published corpus, not merely the headline numbers
        "model_parameters", "data_cutoff", "corpus_manifest_hash",
        # V9.5 eval H6 — and covers what the ladder actually READ, so a
        # decision cannot silently imply it evaluated the corpus bytes
        "evaluation_source")}
    # S-5: a REPLAY decision additionally binds its evidence class and the
    # archive bytes it read. These keys are added ONLY when present, never
    # as nulls — an unconditional key would change the canonical bytes of
    # every existing MLS decision and so change its content hash, breaking
    # the dedupe that makes those rows immutable.
    for extra in ("evidence", "evaluated_rung"):
        if rec.get(extra) is not None:
            core[extra] = rec[extra]
    return _canonical(core)


def _decision_content_hash(rec: dict) -> str:
    return hashlib.sha256(_decision_canonical(rec).encode()).hexdigest()


def _active_decision(spec: LadderSpec | None = None):
    """The newest APPROVED decision for this model, or None."""
    spec = spec or MLS_LADDER_SPEC
    from src.live.models import ModelApprovalDecision
    s = get_session()
    try:
        return (s.query(ModelApprovalDecision)
                .filter_by(model_version_name=spec.model_name, approved=True)
                .order_by(ModelApprovalDecision.id.desc()).first())
    finally:
        s.close()


def ensure_approval_decision(corpus_version: str | None = None,
                             n_boot: int = 1000, force: bool = False,
                             allow_create: bool = True,
                             spec: LadderSpec | None = None,
                             report: dict | None = None,
                             policy=None,
                             policy_version: str | None = None,
                             evidence: dict | None = None,
                             evaluation_source: str | None = None,
                             evaluation_source_note: str | None = None,
                             activation_route: str | None = None,
                             rung: str | None = None) -> dict:
    """LOAD the active approval decision, or (only when none exists, or
    force=True) run the CI evaluator and persist a new IMMUTABLE one, then
    set approved_for_shadow FROM it (V9 eval F1/F10; V9.1 eval F8). Boot
    LOADS rather than recomputes, so the approving decision does not drift
    as the mutable database accumulates data — a re-evaluation is an
    explicit force=True operator action. Deduped by content hash.

    `corpus_version` defaults to the newest PUBLISHED corpus. Boot passed
    nothing, so every decision recorded corpus_version=null and the
    evidence contract's "this model was approved against THIS frozen
    corpus" binding never existed in practice. Resolving it here means
    the binding happens by default rather than by an operator remembering
    a version string; it stays null, honestly, until a corpus is
    published.

    COMPETITION-KEYED (S-5). `spec` defaults to MLS, so the MLS decision
    path is unchanged. The optional arguments exist so a prior-season
    REPLAY decision reuses this same machinery — the same immutability,
    the same content hash, the same fail-closed boot — instead of a
    parallel implementation:

      `report`             a precomputed ladder report (the replay's),
                           used INSTEAD of reading this competition's live
                           plane. Supplying it is the only way a decision
                           can be made from archive bytes.
      `policy`             the approval predicate. Defaults to
                           `shadow_approval_policy`; the replay path
                           passes the stricter `replay_approval_policy`.
      `policy_version`     names which of those decided, so no record is
                           ambiguous about the bar it cleared.
      `evidence`           the evidence-class block (REPLAYED + the
                           archive sha256), covered by the content hash.
      `evaluation_source`  what the ladder actually READ.
    """
    if not plane_ready():
        return {"error": "dormant"}
    spec = spec or MLS_LADDER_SPEC
    model_mod = spec.model
    policy = policy or shadow_approval_policy
    policy_version = policy_version or APPROVAL_POLICY_VERSION
    activation_route = activation_route or (
        f"POST /api/admin/{spec.slug}/approval/activate")
    if corpus_version is None:
        corpus_version = spec.latest_corpus_version()
    current_engine = model_mod.engine_signature()["signature_hash"]
    if not force:
        existing = _active_decision(spec)
        # load only a COMPLETE decision computed under the CURRENT engine
        # (V9.2): a model change (new engine signature) or a pre-V9.1.2 row
        # falls through so a fresh decision evaluates what actually ships —
        # the win% blend must not be authorized by an M2-only decision
        if existing is not None and existing.decision_document:
            try:
                doc_engine = json.loads(
                    existing.decision_document).get("engine_signature")
            except (ValueError, TypeError):
                doc_engine = None
            # REVISION-ONLY DRIFT is not an engine change. engine_signature
            # hashes the git revision, so every deploy moves it — including
            # a migration or a docs commit that cannot touch the model.
            # Comparing hashes strictly therefore failed this match on any
            # deploy and boot fell closed, disarming the plane: four times
            # on 2026-08-02/03, once from a docs-only PR. The replay path
            # already solved this (model_mls.engine_matches): rehash under
            # the revision the record stored, and if THAT reproduces the
            # stored hash then only the revision moved. A genuine source,
            # constant or runtime change still fails both arms, so this
            # narrows nothing — it distinguishes "the repo moved" from
            # "the engine changed", which strict equality could not.
            #
            # A row with no recorded revision (written before the column
            # existed) has no second arm and falls through, which is
            # exactly the previous behaviour. Missing evidence stays
            # missing evidence.
            engine_ok = doc_engine == current_engine
            if not engine_ok and existing.code_revision:
                matcher = getattr(model_mod, "engine_matches", None)
                if matcher is not None:
                    ok, _drift = matcher(doc_engine, existing.code_revision)
                    engine_ok = bool(ok)
            # A change in the corpus binding must also fall through. The
            # active decision recorded corpus_version=null; publishing a
            # corpus does not retroactively bind it, and silently
            # returning the unbound row would leave the binding
            # permanently unreachable without an operator force.
            if (engine_ok
                    and existing.corpus_version == corpus_version):
                # heal a pre-migration row on the EXACT-match path only.
                # An exact signature match means the running revision is
                # the one that produced this row, so recording it is
                # provenance we can prove rather than assume. On the
                # second arm the revision is by definition already there.
                if doc_engine == current_engine and not existing.code_revision:
                    rev = model_mod.engine_signature().get("code_revision")
                    if rev:
                        _s = get_session()
                        try:
                            _row = _s.get(type(existing), existing.id)
                            if _row is not None and not _row.code_revision:
                                _row.code_revision = rev
                                _s.commit()
                        finally:
                            _s.close()
                return {"decision_id": existing.id, "approved": True,
                        "loaded": True,
                        "content_hash": existing.content_hash,
                        "corpus_version": existing.corpus_version,
                        "reason": "loaded active decision (not recomputed)",
                        "policy_version": existing.policy_version,
                        "n_scored": existing.n_scored}
    if not allow_create:
        # V9.5 eval H6: boot must LOAD an approval and FAIL CLOSED, never
        # mint a replacement for itself. Silently re-approving on every
        # engine change contradicted the governance claim that
        # re-evaluation is an explicit operator action — and since the
        # engine signature includes code_revision, that meant every
        # deploy quietly issued a fresh approval from whatever the
        # mutable database happened to hold.
        model_mod.ensure_model_version(approved_for_shadow=False)
        return {"approval_decision_missing": True, "approved": False,
                "reason": ("no ACTIVE approval decision for the current "
                           f"engine/corpus — operator activation required "
                           f"({activation_route}). "
                           "Shadow runs stay refused until then."),
                "fail_closed": True}
    if report is None:
        report = evaluate_ladder(n_boot=n_boot, spec=spec)
    if report.get("n_scored", 0) == 0:
        return {"error": report.get("note") or "no scorable fixtures"}
    approved, reason = policy(report) if policy is shadow_approval_policy \
        else policy(report, spec)
    rec = approval_record(report, corpus_version=corpus_version, spec=spec,
                          evidence=evidence, rung=rung)
    rec["policy_version"] = policy_version
    rec["approved"] = approved
    rec["decision_reason"] = reason
    rec["engine_signature"] = current_engine   # pins the decision to the
    #                                            exact deployed model (V9.2)
    # V9.3 eval F7: an approval must say WHAT it approved and on what
    # data. Record the exact selected parameters, the engine, the data
    # cutoff, and the published corpus it is bound to (null until one is
    # published — which is itself disclosed rather than hidden).
    rec["model_parameters"] = spec.model_parameters()
    rec["data_cutoff"] = (evidence or {}).get("data_cutoff") \
        or _data_cutoff(spec)
    rec["corpus_manifest_hash"] = _published_corpus_hash(corpus_version)
    # V9.5 eval H6: the corpus binding is a LABEL, not the data the
    # ladder read. evaluate_ladder() reads the mutable live database;
    # it does not load the published corpus bytes. Recording that
    # plainly inside the decision (and therefore inside its content
    # hash) stops the binding from overclaiming what it establishes.
    rec["evaluation_source"] = evaluation_source or "live_database"
    rec["evaluation_source_note"] = evaluation_source_note or (
        "the ladder evaluated CURRENT database state, not the published "
        "corpus bytes; corpus_version/corpus_manifest_hash record which "
        "corpus this decision is filed against, not what it computed from")
    canonical = _decision_canonical(rec)
    chash = hashlib.sha256(canonical.encode()).hexdigest()

    from src.live.models import ModelApprovalDecision, ModelVersion
    s = get_session()
    try:
        mv = s.query(ModelVersion).filter_by(name=spec.model_name).first()
        if mv is None:
            # create the row (unapproved) so the decision can reference it
            model_mod.ensure_model_version(approved_for_shadow=False)
            mv = s.query(ModelVersion).filter_by(name=spec.model_name).first()
        existing = (s.query(ModelApprovalDecision)
                    .filter_by(content_hash=chash).first())
        if existing is None:
            row = ModelApprovalDecision(
                model_version_id=mv.id, model_version_name=spec.model_name,
                eval_version=report.get("eval_version"),
                policy_version=policy_version,
                corpus_version=corpus_version,
                approved_mode="shadow", approved=approved,
                n_scored=report.get("n_scored"),
                metrics_json=json.dumps(rec["metrics"]),
                edge_json=json.dumps(rec["edge_vs_baseline"]),
                limitations_json=json.dumps(rec["limitations"]),
                report_json=json.dumps(report)[:200_000],
                decision_document=canonical,
                approved_by="automated-eval", content_hash=chash,
                # provenance for the revision-drift second arm above; not
                # part of content_hash and not in decision_document
                code_revision=(model_mod.engine_signature()
                               .get("code_revision") or None),
                created_at=datetime.now(timezone.utc))
            s.add(row)
            s.commit()
            decision_id = row.id
        else:
            # heal a pre-V9.1.2 row that stored the hash but not the
            # canonical document it covers — sha256(document) still equals
            # the stored content_hash, so this only fills a NULL, it never
            # alters the decision (V9.1 eval F4/F8)
            if not existing.decision_document:
                existing.decision_document = canonical
                s.commit()
            # same heal for the revision, and it is provably the RIGHT
            # revision rather than merely the current one: dedup matched
            # on content_hash, content_hash covers engine_signature, and
            # the signature hashes code_revision — so a row with this
            # hash could only have been produced under the revision
            # running now. Fills a NULL, never overwrites a recorded one.
            if not existing.code_revision:
                rev = model_mod.engine_signature().get("code_revision")
                if rev:
                    existing.code_revision = rev
                    s.commit()
            decision_id = existing.id
    finally:
        s.close()
    # flip the model_version flag FROM the persisted decision
    model_mod.ensure_model_version(approved_for_shadow=approved)
    return {"decision_id": decision_id, "approved": approved,
            "reason": reason, "content_hash": chash,
            "policy_version": policy_version,
            "model_version": spec.model_name,
            "competition": spec.slug,
            "evidence_class": (evidence or {}).get("evidence_class"),
            "n_scored": report.get("n_scored"),
            "edge_vs_baseline": rec["edge_vs_baseline"]}


def latest_approved_decision_id(spec: LadderSpec | None = None
                                ) -> int | None:
    """The newest APPROVED shadow decision id for THIS competition's
    model, stamped on each run so a run points at the exact record that
    authorized it (V9 eval F10)."""
    if not plane_ready():
        return None
    spec = spec or MLS_LADDER_SPEC
    from src.live.models import ModelApprovalDecision
    s = get_session()
    try:
        row = (s.query(ModelApprovalDecision)
               .filter_by(model_version_name=spec.model_name, approved=True)
               .order_by(ModelApprovalDecision.id.desc()).first())
        return row.id if row else None
    finally:
        s.close()


def current_approval_decision(spec: LadderSpec | None = None) -> dict:
    """The persisted approval decision the runtime operates under, read as
    STORED — never a recomputation (pre-slate evidence contract). Returns
    the immutable row's fields (incl. its own content hash) or
    `{approval_decision_missing: True}` when none exists; it must never
    invent a decision. corpus_manifest_hash is read from the decision
    DOCUMENT — it is covered by the content hash, so it is the tamper-
    evident copy — and stays null while the approval is bound to no
    published corpus. It was previously hardcoded None beside a comment
    saying no corpus existed yet; once one was published that comment
    became false and the endpoint reported an unbound approval that was
    in fact bound.

    SCOPED TO ONE MODEL (S-5). This query used to filter on `approved=True`
    alone and take the newest row. With MLS the only approved model that
    was harmless; the moment a second competition earns an approval it
    becomes a cross-league leak — /api/mls/approval would report whichever
    league was approved most recently. `spec` defaults to MLS, so the MLS
    endpoint keeps returning the MLS decision and each league reads its
    own."""
    if not plane_ready():
        return {"approval_decision_missing": True, "reason": "dormant"}
    spec = spec or MLS_LADDER_SPEC
    from src.live.models import ModelApprovalDecision
    s = get_session()
    try:
        row = (s.query(ModelApprovalDecision)
               .filter_by(model_version_name=spec.model_name, approved=True)
               .order_by(ModelApprovalDecision.id.desc()).first())
        if row is None:
            return {"approval_decision_missing": True}
        edge = json.loads(row.edge_json) if row.edge_json else {}
        ci = edge.get("ci95") or [None, None]
        manifest_hash = None
        if row.decision_document:
            try:
                manifest_hash = json.loads(
                    row.decision_document).get("corpus_manifest_hash")
            except (ValueError, TypeError):
                manifest_hash = None
        return {
            "decision_id": row.id,
            "content_hash": row.content_hash,
            "model_version": row.model_version_name,
            "corpus_version": row.corpus_version,
            "corpus_manifest_hash": manifest_hash,
            "evaluation_version": row.eval_version,
            "approval_policy_version": row.policy_version,
            "approved_mode": row.approved_mode,
            "approved": row.approved,
            "n_scored": row.n_scored,
            "edge_vs_baseline": edge.get("delta_log_loss"),
            "ci_low": ci[0] if len(ci) == 2 else None,
            "ci_high": ci[1] if len(ci) == 2 else None,
            "edge_significant": edge.get("significant"),
            "approved_at": (row.created_at.isoformat()
                            if row.created_at else None),
        }
    finally:
        s.close()


def boot_shadow_flag(model_mod, model_name: str) -> bool:
    """What approved_for_shadow should be at BOOT for a replay plane.

    The V9.5 H6 rule said boot force-darks these planes, and while the
    strict-equality engine check existed that was the only safe answer:
    every deploy moved the git revision, the hash never matched, and an
    armed flag at boot could not be distinguished from a stale one. #59
    changed that for the runtime path; this extends the SAME two-arm rule
    to boot, because at the operator's release cadence the force-dark
    was costing a manual rearm per deploy per plane — ~40 decision rows
    of pure churn in four days, and a plane silently dark whenever a
    restart landed between rearms.

    The rule, unchanged from #59: the latest approved shadow decision's
    stored engine hash must reproduce either under the CURRENT revision
    (nothing moved) or under the revision THE DECISION RECORDED (only
    the repo moved). A genuine source, constant or runtime change fails
    both arms and the plane stays dark. No decision, no document, no
    stored revision, or ANY error — dark. Fail-closed is preserved; what
    changes is only that "the repo moved" no longer counts as evidence
    the engine did.

    Generic on purpose: rehashes via model_mod.engine_signature(
    code_revision=...), which every plane ships, rather than
    engine_matches, which liga mx does not.
    """
    import json as _json
    try:
        from src.live.db import get_session
        from src.live.models import ModelApprovalDecision
        s = get_session()
        try:
            row = (s.query(ModelApprovalDecision)
                   .filter_by(model_version_name=model_name,
                              approved=True, approved_mode="shadow")
                   .order_by(ModelApprovalDecision.id.desc())
                   .first())
            if row is None or not row.decision_document:
                return False
            doc = _json.loads(row.decision_document)
            stored = doc.get("engine_signature")
            rev = row.code_revision
        finally:
            s.close()
        if not stored:
            return False
        current = model_mod.engine_signature()["signature_hash"]
        if stored == current:
            return True
        if rev:
            again = model_mod.engine_signature(
                code_revision=rev)["signature_hash"]
            return again == stored
        return False
    except Exception as exc:
        print(f"[boot-flag] {model_name}: fail closed "
              f"({type(exc).__name__}: {str(exc)[:90]})")
        return False
