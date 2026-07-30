"""Prior-season REPLAY ladders for the dark leagues (backlog S-5).

WHAT THIS IS, AND WHAT IT IS NOT
================================
This runs each league's own model module up the M0-M2 ladder on that
league's COMPLETED PRIOR SEASON, retrieved from ESPN and archived under
`research_archive/ladder_<league>_prior_season_<date>.json`.

The result is **REPLAYED** evidence, and every artifact, decision
document and log line this module produces carries that word. The
evidence hierarchy (docs/V9/CALIBRATION.md, carried from V6/V7) is:

    MEASURED (frozen pre-event, scored on outcome)
      > REPLAYED (backtest over recorded data)
        > PILOT (small-n live anecdote)

A prior-season replay is the WEAKEST form of REPLAYED evidence this
platform produces, weaker even than MLS's live-plane ladder, for a
reason that is structural rather than statistical: it reads a DIFFERENT
SEASON from the one an approval would license. Squads turn over between
seasons, and in EPL and La Liga the club list itself changes through
promotion and relegation — three clubs each season have no history in
the fitted ratings at all.

So a replay result may NEVER be presented beside MLS's standing
prospective figure (+0.0269, n=177, CI [-0.0050, +0.0596], NOT
significant) as though the two were the same kind of number. MLS's is
prospective. This is not.

WHY THE LIVE PLANE IS NOT TOUCHED
=================================
Prior-season history is research input, not platform data, so it stays a
file. Nothing here writes a Fixture row, and the live `Fixture` table
keeps holding exactly the competitions the platform runs. A useful side
effect: the ladder becomes reproducible from bytes whose sha256 the
report records, rather than from mutable database state — which is the
weakness V9.5 H6 named in the live-plane ladder.

THE STAGE-1 GATE
================
S-5's falsifier: count the events actually retrieved against the
season's expected length and REPORT THE RATIO. Incomplete history kills
the ladder for that league; `completeness()` decides it and
`run_replay()` refuses rather than quietly fitting on a partial season.
No count is ever reported without its denominator.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

from src.live import model_eval

# The replay is REPLAYED evidence. This constant exists so the string is
# impossible to get wrong or to omit.
EVIDENCE_CLASS = "REPLAYED"

# The rung a replay decision is computed on, ALWAYS.
#
# It is not `model_eval.deployed_variant()`. For MLS that helper returns
# M3 because MLS_XG_RATING_ALPHA > 0 — but the prior-season ARCHIVE
# carries no provider xG at all (ESPN's scoreboard has no xG fields), so
# the replay's M3 rung is numerically identical to M2C and calling it M3
# would claim the xG feature had been evaluated when it was not. And for
# EPL and Liga MX no team-level xG source exists in this stack in the
# first place. So every replay decision is an M2 decision, named as such.
REPLAY_DECISION_RUNG = "M2"

# MLS is a METHODOLOGICAL CONTROL here, never an approval target. Its
# shadow approval is earned on its own LIVE plane; a prior-season replay
# must never be able to mint a second, competing MLS decision — that
# would put a REPLAYED record beside a live one under the same model
# name, which is exactly the blurring the evidence hierarchy forbids.
APPROVAL_FORBIDDEN = {
    "mls-2026": ("MLS holds a LIVE-PLANE shadow approval. A prior-season "
                 "replay is weaker evidence and must never create a "
                 "second decision under the same model name; this "
                 "competition is a methodological control only."),
}
EVIDENCE_NOTE = (
    "prior-season replay: the ladder was fitted and scored on the "
    "league's PREVIOUS completed season, retrieved after the fact from "
    "ESPN. Nothing was frozen before kickoff, so this can never be "
    "MEASURED evidence; and because it reads a DIFFERENT season from the "
    "one an approval licenses (different squads, and for the promoted/"
    "relegated clubs a different club list entirely), it is weaker than "
    "a same-season rolling-origin ladder. It is NOT comparable to any "
    "prospective result.")

# Stage-1 gate: a segment must retrieve at least this fraction of its
# expected fixtures to be fitted at all. Set at 0.98 rather than 1.0
# because a season can genuinely lose a fixture to abandonment, but a
# missing gameweek must never pass silently.
MIN_COMPLETENESS = 0.98

ARCHIVE_DATE = "2026-07-29"


class ReplaySource:
    """Where one competition's prior season lives, and how it segments.

    `regular_slugs` is the load-bearing field. ESPN tags every event with
    a `season.slug`, and that is the provider's own tournament
    discriminator — so the segmentation is read from the data rather than
    guessed from dates. Everything not in `regular_slugs` (Liguilla, MLS
    playoffs, the All-Star game) is EXCLUDED from the ladder and reported
    as an exclusion with its count, never silently mixed into a league
    table.
    """

    def __init__(self, slug: str, espn_league: str, season_label: str,
                 regular_slugs: tuple, expected_per_segment: int,
                 archive_date: str = ARCHIVE_DATE,
                 club_turnover_note: str = ""):
        self.slug = slug
        self.espn_league = espn_league
        self.season_label = season_label
        self.regular_slugs = regular_slugs
        self.expected_per_segment = expected_per_segment
        self.archive_date = archive_date
        self.club_turnover_note = club_turnover_note

    @property
    def archive_path(self) -> str:
        league = self.espn_league.replace(".", "")
        return (f"research_archive/ladder_{league}_prior_season_"
                f"{self.archive_date}.json")


REPLAY_SOURCES = {
    "epl-2026": ReplaySource(
        slug="epl-2026", espn_league="eng.1", season_label="2025-26",
        regular_slugs=("2025-26-english-premier-league",),
        expected_per_segment=380,
        club_turnover_note=(
            "3 clubs are relegated and 3 promoted each season, so ~15% of "
            "the 2026-27 club list has NO rating in this replay's fit")),
    "la-liga-2026": ReplaySource(
        slug="la-liga-2026", espn_league="esp.1", season_label="2025-26",
        regular_slugs=("2025-26-laliga",),
        expected_per_segment=380,
        club_turnover_note=(
            "3 clubs are relegated and 3 promoted each season, so ~15% of "
            "the 2026-27 club list has NO rating in this replay's fit")),
    "liga-mx-2026": ReplaySource(
        slug="liga-mx-2026", espn_league="mex.1",
        season_label="Apertura 2025 + Clausura 2026",
        # TWO tournaments, kept distinct. ESPN gives both `season.year`
        # 2025 (its season-year spans both), so the YEAR cannot separate
        # them and the SLUG must.
        regular_slugs=("torneo-apertura", "torneo-clausura"),
        expected_per_segment=153,
        club_turnover_note=(
            "18 clubs, no promotion/relegation in this period, so the club "
            "list is stable — the turnover risk here is squad churn "
            "between tournaments, not a changed league")),
    # The METHODOLOGICAL CONTROL, not a league to approve. MLS already
    # holds a live-plane approval; running the same replay machinery on
    # MLS's prior season is the only way to see what a prior-season replay
    # is worth on a league whose live answer is known.
    "mls-2026": ReplaySource(
        slug="mls-2026", espn_league="usa.1", season_label="2025",
        regular_slugs=("regular-season",),
        expected_per_segment=510,
        club_turnover_note=(
            "MLS expanded between 2025 and 2026; the 2025 fit cannot rate "
            "a 2026 expansion club")),
}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def load_archive(source: ReplaySource) -> tuple[dict, str]:
    """Read a league's archived prior season. Returns (document, sha256).

    The sha256 is over the exact bytes on disk and travels into the
    report and the approval decision, so a decision names the input it
    was computed from — the property the live-plane ladder lacks.
    """
    path = os.path.join(_repo_root(), source.archive_path)
    with open(path, "rb") as fh:
        raw = fh.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def segments(doc: dict, source: ReplaySource) -> dict:
    """{segment_key: [events]} for the REGULAR-SEASON segments only, in
    kickoff order, plus the exclusions as counts."""
    keep: dict[str, list] = {}
    excluded: dict[str, int] = {}
    for ev in doc.get("events") or []:
        slug = ev.get("season_slug")
        if slug in source.regular_slugs:
            keep.setdefault(slug, []).append(ev)
        else:
            excluded[slug] = excluded.get(slug, 0) + 1
    for rows in keep.values():
        rows.sort(key=lambda e: e.get("kickoff_utc") or "")
    return {"segments": keep, "excluded": excluded}


def completeness(doc: dict, source: ReplaySource) -> dict:
    """STAGE 1 (S-5's falsifier). Count what we actually got against what
    the season should contain, per segment, and say whether the ladder may
    proceed. Every count carries its denominator."""
    split = segments(doc, source)
    per_segment = {}
    for slug in source.regular_slugs:
        got = len(split["segments"].get(slug, []))
        expected = source.expected_per_segment
        ratio = (got / expected) if expected else 0.0
        per_segment[slug] = {
            "retrieved": got,
            "expected": expected,
            "ratio": round(ratio, 4),
            "complete_enough": bool(ratio >= MIN_COMPLETENESS),
        }
    all_ok = bool(per_segment) and all(
        v["complete_enough"] for v in per_segment.values())
    excluded_total = sum(split["excluded"].values())
    return {
        "competition": source.slug,
        "espn_league": source.espn_league,
        "season_label": source.season_label,
        "archive_path": source.archive_path,
        "min_completeness": MIN_COMPLETENESS,
        "per_segment": per_segment,
        "regular_season_total": sum(
            v["retrieved"] for v in per_segment.values()),
        "excluded_non_regular_season": split["excluded"],
        "excluded_non_regular_season_total": excluded_total,
        "excluded_reason": (
            "knockout/exhibition events (Liguilla, MLS playoffs, All-Star) "
            "are not league fixtures and are never mixed into a league "
            "table; they are reported, not dropped in silence"),
        "winner_flag_disagreements": (doc.get("counts") or {}).get(
            "winner_flag_disagreements"),
        "gate": "PASS" if all_ok else "FAIL",
        "gate_meaning": (
            "PASS = the ladder may be fitted on this history. FAIL = "
            "incomplete history KILLS the replay for this league; it "
            "waits for live accumulation rather than being fitted on a "
            "partial season."),
    }


def _rows(events: list) -> list:
    """Duck-typed fixture rows for the ladder.

    The ladder reads five attributes and a stable id. ESPN team ids are
    used directly as the team key — provider-stable identity, never names
    (AGENTS.md §13). No database row is created or needed.
    """
    out = []
    for ev in events:
        kickoff = ev.get("kickoff_utc")
        if not kickoff:
            continue
        dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        out.append(SimpleNamespace(
            id=int(ev["espn_event_id"]),
            espn_event_id=str(ev["espn_event_id"]),
            current_kickoff_utc=dt,
            home_team_id=int(ev["home_team_id"]),
            away_team_id=int(ev["away_team_id"]),
            home_goals=int(ev["home_goals"]),
            away_goals=int(ev["away_goals"]),
            segment=ev.get("season_slug"),
        ))
    out.sort(key=lambda f: f.current_kickoff_utc)
    return out


def _segment_prior_fn(rows, i):
    """History slice that RESETS at a segment boundary: only fixtures from
    the same tournament inform a prediction."""
    seg = rows[i].segment
    return [f for f in rows[:i] if f.segment == seg]


def run_replay(competition: str, n_boot: int = 1000, seed: int = 12345,
               segment_mode: str = "per_segment") -> dict:
    """Run one competition's ladder on its archived prior season.

    `segment_mode`:
      "per_segment"  each regular-season segment gets its OWN ladder.
                     For a single-tournament league (EPL, La Liga, MLS)
                     that is simply the season.
      "span"         one ladder over all segments concatenated, so
                     ratings carry across the tournament boundary under
                     the recency half-life. Only meaningful for Liga MX.

    Refuses (rather than fitting) when the stage-1 gate fails.
    """
    source = REPLAY_SOURCES.get(competition)
    if source is None:
        return {"error": f"no replay source for {competition!r}",
                "known": sorted(REPLAY_SOURCES)}
    spec = model_eval.ladder_spec(competition)
    doc, sha = load_archive(source)
    gate = completeness(doc, source)
    base = {
        "competition": competition,
        "model_version": spec.model_name,
        "evidence_class": EVIDENCE_CLASS,
        "evidence_note": EVIDENCE_NOTE,
        "club_turnover_note": source.club_turnover_note,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive": {"path": source.archive_path, "sha256": sha,
                    "fetched_at": doc.get("fetched_at"),
                    "source": doc.get("source")},
        "stage1_completeness": gate,
        "segment_mode": segment_mode,
        "hyperparameters": {
            "half_life_days": spec.half_life_days(),
            "min_games": spec.min_games(),
            "result_shrink": spec.result_shrink(),
            "calibration_alpha": spec.calibration_alpha(),
            "provenance": (
                "carried from MLS league play as STARTING POINTS and NOT "
                "re-swept on this league's data — see the sweep block when "
                "present, and treat an unswept constant as unmeasured"),
        },
        "rungs": {k: v["desc"] for k, v in spec.ladder.items()},
    }
    if gate["gate"] != "PASS":
        return {**base, "refused": True,
                "reason": ("stage-1 history completeness FAILED — the "
                           "replay is killed for this league rather than "
                           "fitted on a partial season")}
    split = segments(doc, source)
    ladders = {}
    if segment_mode == "span":
        rows = _rows([e for slug in source.regular_slugs
                      for e in split["segments"].get(slug, [])])
        ladders["ALL_SEGMENTS_SPANNING"] = {
            "n_fixtures_input": len(rows),
            **model_eval.ladder_from_fixtures(
                rows, spec=spec, n_boot=n_boot, seed=seed),
        }
    else:
        for slug in source.regular_slugs:
            rows = _rows(split["segments"].get(slug, []))
            if not rows:
                continue
            ladders[slug] = {
                "n_fixtures_input": len(rows),
                **model_eval.ladder_from_fixtures(
                    rows, spec=spec, n_boot=n_boot, seed=seed),
            }
    return {**base, "ladders": ladders,
            "eligibility_note": (
                "n_scored < n_fixtures_input by construction: a fixture is "
                "scored only when BOTH teams already have >= min_games "
                "prior completed games, so the opening rounds of every "
                "segment are not scorable by the ratings rungs and are "
                "excluded from ALL rungs to keep the comparison on "
                "identical ground")}


def split_season_verdict(competition: str = "liga-mx-2026",
                         n_boot: int = 1000, seed: int = 12345,
                         rung: str = "M2") -> dict:
    """Liga MX's open split-season question, decided on evidence.

    Its builder flagged it and nobody answered it: `fit()` spans the
    Apertura->Clausura boundary under a 90-day half-life, so Clausura
    ratings carry Apertura history. Whether they SHOULD is a modelling
    choice that was stated rather than tested.

    This runs the ladder BOTH WAYS on the same archived history and
    compares them where a comparison is legitimate: on the fixtures BOTH
    policies can score, paired, with a bootstrap interval. Pairing
    matters — spanning makes Clausura's opening rounds predictable that
    resetting cannot score at all, so an unpaired comparison would
    confound the policy with a larger sample.

    THE RAW COMPARISON IS STILL CONFOUNDED, and the verdict does not rest
    on it. Spanning changes TWO things at once: teams keep their ratings
    across the boundary (the question), and the league-level parameters
    (league goals-per-game, the venue split) are fitted on roughly twice
    as much history (not the question). The M0 rung isolates the second
    effect exactly, because M0 uses NO team information whatsoever — so
    any span-vs-reset difference at M0 is purely the league-parameter
    effect.

    The verdict therefore rests on a DIFFERENCE IN DIFFERENCES: the M2
    gain from spanning MINUS the M0 gain from spanning, paired and
    bootstrapped. That quantity is the rating-carryover effect with the
    league-parameter effect subtracted out, and it is the only one of
    these numbers that answers the question that was asked.
    """
    source = REPLAY_SOURCES.get(competition)
    if source is None:
        return {"error": f"no replay source for {competition!r}"}
    if len(source.regular_slugs) < 2:
        return {"competition": competition,
                "not_applicable": True,
                "reason": ("this league has a single regular-season "
                           "segment, so there is no boundary to span")}
    spec = model_eval.ladder_spec(competition)
    doc, sha = load_archive(source)
    gate = completeness(doc, source)
    if gate["gate"] != "PASS":
        return {"competition": competition, "refused": True,
                "stage1_completeness": gate}
    split = segments(doc, source)
    rows = _rows([e for slug in source.regular_slugs
                  for e in split["segments"].get(slug, [])])

    keys_span, scores_span = model_eval.score_rows(rows, spec=spec)
    keys_reset, scores_reset = model_eval.score_rows(
        rows, spec=spec, prior_fn=_segment_prior_fn)

    span_by_key = dict(zip(keys_span, scores_span))
    reset_by_key = dict(zip(keys_reset, scores_reset))
    shared = [k for k in keys_span if k in reset_by_key]
    span_rows = [span_by_key[k] for k in shared]
    reset_rows = [reset_by_key[k] for k in shared]
    paired = model_eval.paired_edge(span_rows, reset_rows, rung=rung,
                                   n_boot=n_boot, seed=seed)
    # the no-team-information control: any span-vs-reset difference at M0
    # is the league-parameter effect, not rating carryover
    baseline = model_eval.paired_edge(span_rows, reset_rows, rung="M0",
                                      n_boot=n_boot, seed=seed)
    did = _difference_in_differences(span_rows, reset_rows, rung, "M0",
                                     n_boot=n_boot, seed=seed)
    # seed sensitivity: a significance verdict whose interval bound sits on
    # zero is a bootstrap artifact, not a finding. Re-run the DiD across
    # several seeds and report how often it is significant, with the
    # denominator.
    seeds = (seed, 1, 2, 7, 99, 2026, 555, 31337, 424242, 8)
    sig_count = sum(
        1 for sd in seeds
        if _difference_in_differences(span_rows, reset_rows, rung, "M0",
                                      n_boot=n_boot,
                                      seed=sd).get("significant"))

    # The verdict rests on the DiD, not the raw comparison.
    verdict = "INDISTINGUISHABLE"
    if did.get("significant") and sig_count == len(seeds):
        verdict = "SPAN" if did.get("delta", 0) > 0 else "RESET"
    return {
        "no_team_info_control_M0": baseline,
        "difference_in_differences": did,
        "did_meaning": (
            f"the {rung} gain from spanning MINUS the M0 gain from "
            f"spanning. M0 carries no team information, so its "
            f"span-vs-reset difference is purely the effect of fitting the "
            f"league goals-per-game and venue split on more history. "
            f"Subtracting it leaves the RATING-CARRYOVER effect, which is "
            f"the question actually asked."),
        "did_seed_stability": {
            "significant_in": sig_count,
            "seeds_tried": len(seeds),
            "note": ("a significance flag that depends on the bootstrap "
                     "seed is an artifact; the verdict requires ALL seeds "
                     "to agree"),
        },
        "competition": competition,
        "question": ("should ratings carry across the Apertura->Clausura "
                     "boundary (SPAN) or reset at it (RESET)?"),
        "evidence_class": EVIDENCE_CLASS,
        "evidence_note": EVIDENCE_NOTE,
        "archive": {"path": source.archive_path, "sha256": sha},
        "rung_compared": rung,
        "half_life_days": spec.half_life_days(),
        "n_scored_span_only": len(keys_span),
        "n_scored_reset_only": len(keys_reset),
        "n_paired": len(shared),
        "denominator_note": (
            f"{len(shared)} fixtures could be scored under BOTH policies, "
            f"out of {len(keys_span)} scorable when spanning and "
            f"{len(keys_reset)} when resetting; only the shared set is "
            f"compared, because only there is the policy the sole "
            f"difference"),
        "paired_edge_span_minus_reset": paired,
        "raw_comparison_caveat": (
            "this raw edge is CONFOUNDED and the verdict does not rest on "
            "it: spanning changes both the rating carryover and the amount "
            "of history the league-level parameters are fitted on. See "
            "difference_in_differences."),
        "sign_convention": ("positive delta = SPANNING has the lower log "
                            "loss, i.e. carrying ratings across the "
                            "boundary is better"),
        "verdict": verdict,
        "verdict_meaning": {
            "SPAN": ("the rating-carryover effect is positive and "
                     "significant under every seed tried — the data "
                     "prefers carrying ratings across the boundary"),
            "RESET": ("the rating-carryover effect is negative and "
                      "significant under every seed tried — the data "
                      "prefers resetting ratings at the boundary"),
            "INDISTINGUISHABLE": (
                "the rating-carryover interval spans zero, so the two "
                "policies are NOT distinguishable on this history. The "
                "SIMPLER one therefore wins by default: keep the current "
                "spanning fit, which is what the shared machinery already "
                "does. Resetting would add a per-tournament special case "
                "that buys no measured improvement."),
        }[verdict],
    }


def _difference_in_differences(span_rows: list, reset_rows: list,
                               rung: str, control_rung: str,
                               n_boot: int = 1000,
                               seed: int = 12345) -> dict:
    """(rung gain from spanning) - (control_rung gain from spanning),
    paired and bootstrapped on the same fixtures.

    Isolates the rating-carryover effect from the league-parameter effect.
    Positive = spanning helps the ratings rung MORE than it helps the
    no-team-information baseline.
    """
    import numpy as np
    n = len(span_rows)
    if n == 0 or n != len(reset_rows):
        return {"n": n, "error": "unpaired or empty score lists"}

    def gain(r):
        return np.array([reset_rows[j][r]["log_loss"]
                         - span_rows[j][r]["log_loss"] for j in range(n)])

    did = gain(rung) - gain(control_rung)
    rng = np.random.default_rng(seed)
    boot = np.array([float(np.mean(did[rng.integers(0, n, n)]))
                     for _ in range(n_boot)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {
        "rung": rung, "control_rung": control_rung, "n_paired": n,
        "delta": round(float(np.mean(did)), 5),
        "ci95": [round(float(lo), 5), round(float(hi), 5)],
        "significant": bool(lo > 0 or hi < 0),
        "n_bootstrap": n_boot, "seed": seed,
    }


def sweep(competition: str, half_lives=(30.0, 60.0, 90.0, 180.0, 365.0),
          shrinks=(6.0, 12.0, 24.0, 48.0), n_boot: int = 0,
          seed: int = 12345) -> dict:
    """Re-sweep the two carried MLS constants on THIS league's own history.

    The shrinkage and recency half-life in every dark league's model
    module are STARTING POINTS copied from MLS (their own docstrings say
    so). This measures what they are worth here.

    DIAGNOSTIC ONLY — read the caveat. A constant chosen on this sample
    and then evaluated on this same sample produces an optimistic number,
    exactly the V9.3 F8 problem MLS's own approval discloses. So the
    approval decisions are NOT computed with swept values: they use the
    carried constants, which is also what the deployed model would
    actually run. The sweep says what a re-sweep would buy and whether
    the carried value is defensible, nothing more.
    """
    source = REPLAY_SOURCES.get(competition)
    if source is None:
        return {"error": f"no replay source for {competition!r}"}
    spec = model_eval.ladder_spec(competition)
    doc, sha = load_archive(source)
    gate = completeness(doc, source)
    if gate["gate"] != "PASS":
        return {"competition": competition, "refused": True,
                "stage1_completeness": gate}
    split = segments(doc, source)
    carried = {"half_life_days": spec.half_life_days(),
               "shrink_games": spec.ladder["M2"]["shrink"]}
    grid = []
    for hl in half_lives:
        for sh in shrinks:
            # a spec clone whose M2/M2C rungs carry the swept shrink; the
            # half-life is patched on the spec, never on the model module
            # (model_mls.py in particular may not be edited)
            ladder = {k: dict(v) for k, v in spec.ladder.items()}
            for rung in ("M2", "M2C", "M3"):
                if rung in ladder:
                    ladder[rung]["shrink"] = sh
            probe = model_eval.LadderSpec(
                slug=spec.slug, model_module=spec.model, ladder=ladder,
                edge_pairs=spec.edge_pairs,
                future_rungs=spec.future_rungs,
                calibration_alpha_fn=spec.calibration_alpha,
                xg_map_fn=spec.xg_map_fn, label=spec.label)
            probe.half_life_days = lambda _hl=hl: _hl
            per_segment = {}
            for slug in source.regular_slugs:
                rows = _rows(split["segments"].get(slug, []))
                if not rows:
                    continue
                rep = model_eval.ladder_from_fixtures(
                    rows, spec=probe, n_boot=max(n_boot, 1), seed=seed)
                e = (rep.get("edges") or {}).get("M2_vs_M0") or {}
                per_segment[slug] = {
                    "n_scored": rep.get("n_scored"),
                    "m2_log_loss": (rep.get("variants") or {}).get(
                        "M2", {}).get("log_loss"),
                    "m2_vs_m0": e.get("delta_log_loss"),
                }
            grid.append({"half_life_days": hl, "shrink_games": sh,
                         "per_segment": per_segment,
                         "mean_m2_vs_m0": round(sum(
                             v["m2_vs_m0"] or 0.0
                             for v in per_segment.values())
                             / max(len(per_segment), 1), 4)})
    best = max(grid, key=lambda g: g["mean_m2_vs_m0"])
    carried_row = next(
        (g for g in grid
         if g["half_life_days"] == carried["half_life_days"]
         and g["shrink_games"] == carried["shrink_games"]), None)
    return {
        "competition": competition,
        "evidence_class": EVIDENCE_CLASS,
        "archive": {"path": source.archive_path, "sha256": sha},
        "carried_from_mls": carried,
        "carried_result": carried_row,
        "best_on_this_history": best,
        "gain_over_carried": (
            round(best["mean_m2_vs_m0"]
                  - (carried_row or {}).get("mean_m2_vs_m0", 0.0), 4)
            if carried_row else None),
        "grid": grid,
        "caveat": (
            "DIAGNOSTIC ONLY. Selecting a constant on this sample and "
            "evaluating it on the same sample is optimistic (V9.3 F8). The "
            "approval decisions use the CARRIED constants, not these, so "
            "no approval is inflated by a selection made on its own "
            "evidence. Treat `best_on_this_history` as an upper bound on "
            "what a re-sweep could buy, not as a measured improvement."),
    }


def replay_evidence_block(competition: str, report: dict) -> dict:
    """The evidence block a replay approval decision carries inside its
    content hash — the class, the note, the archive bytes, and the ladder
    result that justified it."""
    source = REPLAY_SOURCES[competition]
    ladders = report.get("ladders") or {}
    return {
        "evidence_class": EVIDENCE_CLASS,
        "evidence_note": EVIDENCE_NOTE,
        "replay_scope": "prior_season",
        "season_replayed": source.season_label,
        "espn_league": source.espn_league,
        "archive_path": (report.get("archive") or {}).get("path"),
        "archive_sha256": (report.get("archive") or {}).get("sha256"),
        "club_turnover_note": source.club_turnover_note,
        "stage1_completeness": report.get("stage1_completeness"),
        "segment_mode": report.get("segment_mode"),
        "ladder_result": {
            seg: {"n_scored": v.get("n_scored"),
                  "variants": v.get("variants"),
                  "edges": v.get("edges")}
            for seg, v in ladders.items()},
        "data_cutoff": _replay_cutoff(competition),
        "not_comparable_to": (
            "any prospective/MEASURED figure, including MLS's standing "
            "live-plane result — that one is prospective, this is a "
            "prior-season replay"),
    }


def _replay_cutoff(competition: str) -> str | None:
    """The last kickoff in the replayed history — the data boundary."""
    source = REPLAY_SOURCES.get(competition)
    if source is None:
        return None
    doc, _sha = load_archive(source)
    split = segments(doc, source)
    stamps = [e.get("kickoff_utc") for slug in source.regular_slugs
              for e in split["segments"].get(slug, [])
              if e.get("kickoff_utc")]
    return max(stamps) if stamps else None


def approval_ladder(competition: str, n_boot: int = 1000,
                    seed: int = 12345) -> tuple[dict, dict]:
    """The ONE ladder an approval decision is computed from, plus its
    evidence block.

    Uses `segment_mode="span"`, i.e. a single ladder over all of the
    league's regular-season history — because that is what the league's
    `fit()` actually does, and an approval must evaluate the model that
    would really run. For a single-tournament league that is just the
    season. For Liga MX it spans the Apertura->Clausura boundary, which
    the split-season verdict examined directly and could not distinguish
    from resetting (so the simpler spanning fit stands).

    Returns `({}, evidence)` with the report empty when the stage-1 gate
    refused, so a caller cannot accidentally approve on a killed replay.
    """
    report = run_replay(competition, n_boot=n_boot, seed=seed,
                        segment_mode="span")
    if report.get("refused") or report.get("error"):
        return {}, {"refused": True, "report": report}
    ladders = report.get("ladders") or {}
    flat = ladders.get("ALL_SEGMENTS_SPANNING") or {}
    evidence = replay_evidence_block(competition, report)
    return flat, evidence


def ensure_replay_approval(competition: str, n_boot: int = 1000,
                           seed: int = 12345,
                           allow_create: bool = False,
                           force: bool = True) -> dict:
    """Create (or load) a shadow-approval decision for `competition` from
    its PRIOR-SEASON REPLAY ladder, through MLS's decision machinery.

    `allow_create` defaults to FALSE. That default is the fail-closed
    posture: nothing that merely imports or boots this module can mint an
    approval, and the only caller that passes True is the operator-gated
    admin route. This mirrors the V9.5 H6 rule that boot LOADS an approval
    and refuses, and that creating one is an explicit operator action.

    The decision it writes is deliberately marked:
      policy_version   shadow-approval-replay-v1  (the STRICTER bar)
      evidence         REPLAYED + the archive path and sha256 it read
      evaluation_source  replay_archive:<path>@<sha256>

    so no reader can mistake it for a live-plane decision, and no reader
    can mistake REPLAYED prior-season evidence for a prospective result.
    """
    if competition in APPROVAL_FORBIDDEN:
        return {"approved": False, "forbidden": True,
                "competition": competition,
                "reason": APPROVAL_FORBIDDEN[competition]}
    try:
        spec = model_eval.ladder_spec(competition)
    except KeyError as exc:
        return {"error": str(exc)}
    flat, evidence = approval_ladder(competition, n_boot=n_boot, seed=seed)
    if evidence.get("refused"):
        return {"approved": False, "refused": True,
                "reason": ("stage-1 history completeness FAILED — no "
                           "approval may be computed from a killed replay"),
                "stage1": (evidence.get("report") or {}).get(
                    "stage1_completeness")}
    if not flat:
        return {"approved": False,
                "reason": "replay produced no scorable ladder"}
    source_ref = (f"replay_archive:{evidence.get('archive_path')}"
                  f"@{(evidence.get('archive_sha256') or '')[:16]}")
    return model_eval.ensure_approval_decision(
        spec=spec, report=flat,
        policy=model_eval.replay_approval_policy,
        policy_version=model_eval.REPLAY_APPROVAL_POLICY_VERSION,
        evidence=evidence,
        evaluation_source=source_ref,
        evaluation_source_note=(
            "the ladder read ARCHIVED PRIOR-SEASON bytes (path + sha256 "
            "recorded above and covered by this decision's content hash), "
            "NOT this competition's live plane and NOT a published corpus. "
            "This is REPLAYED evidence about a DIFFERENT season from the "
            "one the approval licenses."),
        activation_route=(
            f"POST /api/admin/{competition}/replay-approval/activate"),
        rung=REPLAY_DECISION_RUNG,
        allow_create=allow_create, force=force,
        corpus_version=None, n_boot=n_boot)


def approval_preview(competition: str, n_boot: int = 1000,
                     seed: int = 12345) -> dict:
    """What the replay policy WOULD decide, computing and persisting
    nothing. Read-only: this is what the public endpoint serves, so the
    decision can be inspected before any operator acts on it."""
    try:
        spec = model_eval.ladder_spec(competition)
    except KeyError as exc:
        return {"error": str(exc)}
    flat, evidence = approval_ladder(competition, n_boot=n_boot, seed=seed)
    if evidence.get("refused") or not flat:
        return {"competition": competition, "would_approve": False,
                "reason": "stage-1 refused or no scorable ladder",
                "evidence_class": EVIDENCE_CLASS}
    approved, reason = model_eval.replay_approval_policy(flat, spec)
    return {
        "competition": competition,
        "model_version": spec.model_name,
        "approval_forbidden": APPROVAL_FORBIDDEN.get(competition),
        "evidence_class": EVIDENCE_CLASS,
        "evidence_note": EVIDENCE_NOTE,
        "policy_version": model_eval.REPLAY_APPROVAL_POLICY_VERSION,
        "policy_note": (
            "STRICTER than the MLS live-plane policy: a replay reads a "
            "different season, so M2 must BEAT its own M0 on a point "
            "estimate, not merely fail to lose significantly"),
        "evaluated_rung": REPLAY_DECISION_RUNG,
        "evaluated_rung_note": (
            "always M2. The archive carries NO provider xG (ESPN's "
            "scoreboard has no xG fields), so a replay cannot evaluate an "
            "xG rung — for MLS an M3 label would falsely imply it had, and "
            "for EPL/Liga MX no team-level xG source exists at all."),
        "n_scored": flat.get("n_scored"),
        "variants": flat.get("variants"),
        "edges": flat.get("edges"),
        "archive": {"path": evidence.get("archive_path"),
                    "sha256": evidence.get("archive_sha256")},
        "would_approve": approved,
        "reason": reason,
        "persisted": False,
        "activation_route": (
            f"POST /api/admin/{competition}/replay-approval/activate"),
    }


def aggregate_report(n_boot: int = 1000, seed: int = 12345) -> dict:
    """Every registered league's replay ladder, plus the stage-1 table and
    the Liga MX split-season verdict — the whole S-5 answer in one dict."""
    out = {
        "evidence_class": EVIDENCE_CLASS,
        "evidence_note": EVIDENCE_NOTE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_bootstrap": n_boot,
        "seed": seed,
        "leagues": {},
    }
    for slug in REPLAY_SOURCES:
        try:
            model_eval.ladder_spec(slug)
        except KeyError:
            out["leagues"][slug] = {
                "skipped": "no ladder spec — the league's model module is "
                           "not on this branch"}
            continue
        out["leagues"][slug] = run_replay(slug, n_boot=n_boot, seed=seed)
    out["liga_mx_split_season"] = split_season_verdict(
        "liga-mx-2026", n_boot=n_boot, seed=seed)
    return out
