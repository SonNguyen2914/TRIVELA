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
                xg_by_fixture: dict | None = None) -> dict | None:
    """Ratings + league params under a ladder config. Pure function of
    its inputs; the walk-forward calls it with prior-only slices. Mirrors
    model_mls.fit — including the xG-based rating blend (cfg['xg_alpha'])
    so the M3 rung measures exactly the deployed feature."""
    if not fixtures:
        return None
    from src.live.model_mls import RESULT_SHRINK, XG_SHRINK_GAMES
    # xG weight: an explicit cfg value (offline sweeps) wins; otherwise the
    # M3 rung tracks the deployed config so the ladder measures what ships
    xg_alpha = cfg.get("xg_alpha")
    if xg_alpha is None and cfg.get("xg_from_config"):
        import config as _cfg
        xg_alpha = _cfg.MLS_XG_RATING_ALPHA
    xg_alpha = float(xg_alpha or 0.0)
    # xG carries its OWN, lighter prior than goals (see XG_SHRINK_GAMES);
    # a sweep may override it explicitly
    xg_shrink = float(cfg.get("xg_shrink", XG_SHRINK_GAMES))
    gf, ga, wsum, games = {}, {}, {}, {}
    xgf, xga, xwsum = {}, {}, {}
    wins, draws, losses = {}, {}, {}
    tot_home = tot_away = tot_w = 0.0
    tot_xg = txg_w = 0.0
    xgm = xg_by_fixture or {}
    for f in fixtures:
        if cfg["recency"]:
            days = (as_of - _utc(f.current_kickoff_utc)).total_seconds() / 86400
            w = 0.5 ** (max(days, 0.0) / HALF_LIFE_DAYS)
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


def predict_variant(model: dict, fixture) -> dict | None:
    """Analytic 3-way for one fixture under a fitted variant."""
    lh_v, la_v = model["venue_home"], model["venue_away"]
    if model["use_ratings"]:
        h = model["ratings"].get(fixture.home_team_id)
        a = model["ratings"].get(fixture.away_team_id)
        if h is None or a is None or h["games"] < MIN_GAMES \
                or a["games"] < MIN_GAMES:
            return None
        lam_h = model["league"] * h["attack"] * a["defence"] * lh_v
        lam_a = model["league"] * a["attack"] * h["defence"] * la_v
    else:
        # M0 still needs enough history to be a fair comparison point
        lam_h = model["league"] * lh_v
        lam_a = model["league"] * la_v
    three = analytic_3way(lam_h, lam_a)
    if model.get("calibrate"):
        import config
        from src.live.model_mls import calibrate
        three = calibrate(three, config.MLS_CALIBRATION_ALPHA)
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


def evaluate_ladder(n_boot: int = 1000, seed: int = 12345) -> dict:
    """Rolling-origin evaluation of every evaluable rung with analytic
    scoring and match-cluster bootstrap CIs on the pairwise edges."""
    if not plane_ready():
        return {"error": "dormant"}
    from src.live.model_mls import _completed
    s = get_session()
    try:
        rows = _completed(s)
    finally:
        s.close()
    # provider xG per fixture, loaded once (M3); rungs without xg_alpha
    # ignore it. Keyed by fixture id, so slicing `rows` restricts it too.
    from src.live import mls_stats
    xg_map = mls_stats.team_xg_map()
    # per-fixture per-variant scores (only fixtures every variant can
    # predict, so the comparison is on identical ground)
    per_fixture: list[dict] = []
    for i, f in enumerate(rows):
        prior, as_of = rows[:i], _utc(f.current_kickoff_utc)
        preds = {}
        ok = True
        for name, cfg in LADDER.items():
            m = fit_variant(prior, as_of, cfg, xg_by_fixture=xg_map)
            p = predict_variant(m, f) if m else None
            if p is None:
                ok = False
                break
            preds[name] = p
        if not ok:
            continue
        result = ("home_win" if f.home_goals > f.away_goals else
                  "away_win" if f.away_goals > f.home_goals else "draw")
        per_fixture.append({name: _score_fixture(preds[name], result)
                            for name in LADDER})
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
               "desc": LADDER[name]["desc"]}
        for name in LADDER}

    # match-cluster bootstrap: resample fixtures with replacement
    rng = np.random.default_rng(seed)
    pairs = [("M2", "M0"), ("M2", "M1"), ("M1", "M0"),
             ("M2C", "M2"), ("M2C", "M0"),
             ("M3", "M2C"), ("M3", "M0")]
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
        "future_rungs": FUTURE_RUNGS,
    }


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


def deployed_variant() -> str:
    """The ladder rung that matches the DEPLOYED model: M3 when xG ratings
    are on, else M2C when calibration is on, else M2. The approval
    decision evaluates THIS variant so the persisted edge reflects what
    actually ships."""
    import config
    if config.MLS_XG_RATING_ALPHA > 0:
        return "M3"
    return "M2C" if config.MLS_CALIBRATION_ALPHA > 0 else "M2"


def approval_record(report: dict, corpus_version: str | None = None) -> dict:
    """The model-approval decision record (V8.1 eval Phase 6). Shadow
    approval means 'safe to collect prospective evidence', explicitly
    NOT 'edge established' — and this record never grants a higher mode.
    Evaluates the DEPLOYED variant (M3 when xG ratings are on)."""
    dv = deployed_variant()
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
    return {
        "model_version": MODEL_NAME,
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


def _data_cutoff() -> str | None:
    """The kickoff of the most recent fixture that could inform this
    approval — the data boundary the decision was computed on."""
    from src.live.model_mls import _completed
    s = get_session()
    try:
        rows = _completed(s)
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
        "model_parameters", "data_cutoff", "corpus_manifest_hash")}
    return _canonical(core)


def _decision_content_hash(rec: dict) -> str:
    return hashlib.sha256(_decision_canonical(rec).encode()).hexdigest()


def _active_decision():
    """The newest APPROVED decision for this model, or None."""
    from src.live.models import ModelApprovalDecision
    s = get_session()
    try:
        return (s.query(ModelApprovalDecision)
                .filter_by(model_version_name=MODEL_NAME, approved=True)
                .order_by(ModelApprovalDecision.id.desc()).first())
    finally:
        s.close()


def ensure_approval_decision(corpus_version: str | None = None,
                             n_boot: int = 1000, force: bool = False) -> dict:
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
    published."""
    if not plane_ready():
        return {"error": "dormant"}
    from src.live import model_mls
    if corpus_version is None:
        from src.live import corpus as _c
        corpus_version = _c.latest_published_version()
    current_engine = model_mls.engine_signature()["signature_hash"]
    if not force:
        existing = _active_decision()
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
            # A change in the corpus binding must also fall through. The
            # active decision recorded corpus_version=null; publishing a
            # corpus does not retroactively bind it, and silently
            # returning the unbound row would leave the binding
            # permanently unreachable without an operator force.
            if (doc_engine == current_engine
                    and existing.corpus_version == corpus_version):
                return {"decision_id": existing.id, "approved": True,
                        "loaded": True,
                        "content_hash": existing.content_hash,
                        "corpus_version": existing.corpus_version,
                        "reason": "loaded active decision (not recomputed)",
                        "policy_version": existing.policy_version,
                        "n_scored": existing.n_scored}
    report = evaluate_ladder(n_boot=n_boot)
    if report.get("n_scored", 0) == 0:
        return {"error": report.get("note") or "no scorable fixtures"}
    approved, reason = shadow_approval_policy(report)
    rec = approval_record(report, corpus_version=corpus_version)
    rec["policy_version"] = APPROVAL_POLICY_VERSION
    rec["approved"] = approved
    rec["decision_reason"] = reason
    rec["engine_signature"] = current_engine   # pins the decision to the
    #                                            exact deployed model (V9.2)
    # V9.3 eval F7: an approval must say WHAT it approved and on what
    # data. Record the exact selected parameters, the engine, the data
    # cutoff, and the published corpus it is bound to (null until one is
    # published — which is itself disclosed rather than hidden).
    from src.live import corpus as _corpus
    rec["model_parameters"] = _corpus._model_parameters()
    rec["data_cutoff"] = _data_cutoff()
    rec["corpus_manifest_hash"] = _published_corpus_hash(corpus_version)
    canonical = _decision_canonical(rec)
    chash = hashlib.sha256(canonical.encode()).hexdigest()

    from src.live.models import ModelApprovalDecision, ModelVersion
    s = get_session()
    try:
        mv = s.query(ModelVersion).filter_by(name=MODEL_NAME).first()
        if mv is None:
            # create the row (unapproved) so the decision can reference it
            model_mls.ensure_model_version(approved_for_shadow=False)
            mv = s.query(ModelVersion).filter_by(name=MODEL_NAME).first()
        existing = (s.query(ModelApprovalDecision)
                    .filter_by(content_hash=chash).first())
        if existing is None:
            row = ModelApprovalDecision(
                model_version_id=mv.id, model_version_name=MODEL_NAME,
                eval_version=report.get("eval_version"),
                policy_version=APPROVAL_POLICY_VERSION,
                corpus_version=corpus_version,
                approved_mode="shadow", approved=approved,
                n_scored=report.get("n_scored"),
                metrics_json=json.dumps(rec["metrics"]),
                edge_json=json.dumps(rec["edge_vs_baseline"]),
                limitations_json=json.dumps(rec["limitations"]),
                report_json=json.dumps(report)[:200_000],
                decision_document=canonical,
                approved_by="automated-eval", content_hash=chash,
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
            decision_id = existing.id
    finally:
        s.close()
    # flip the model_version flag FROM the persisted decision
    model_mls.ensure_model_version(approved_for_shadow=approved)
    return {"decision_id": decision_id, "approved": approved,
            "reason": reason, "content_hash": chash,
            "policy_version": APPROVAL_POLICY_VERSION,
            "n_scored": report.get("n_scored"),
            "edge_vs_baseline": rec["edge_vs_baseline"]}


def latest_approved_decision_id() -> int | None:
    """The newest APPROVED shadow decision id, stamped on each run so a
    run points at the exact record that authorized it (V9 eval F10)."""
    if not plane_ready():
        return None
    from src.live.models import ModelApprovalDecision
    s = get_session()
    try:
        row = (s.query(ModelApprovalDecision)
               .filter_by(model_version_name=MODEL_NAME, approved=True)
               .order_by(ModelApprovalDecision.id.desc()).first())
        return row.id if row else None
    finally:
        s.close()


def current_approval_decision() -> dict:
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
    in fact bound."""
    if not plane_ready():
        return {"approval_decision_missing": True, "reason": "dormant"}
    from src.live.models import ModelApprovalDecision
    s = get_session()
    try:
        row = (s.query(ModelApprovalDecision)
               .filter_by(approved=True)
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
