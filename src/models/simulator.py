"""Monte Carlo match simulator.

Runs N Poisson-sampled matches from the xG model, folds in red-card risk,
and returns probabilities for every market type we track plus an
ensemble-style confidence estimate.
"""
from __future__ import annotations

import numpy as np

import config
from src.models.features import stage_uncertainty
from src.models.xg_model import MODEL_VERSION, predict_xg

# --- Knockout continuation (Piece 2) ---------------------------------------
# Extra time = 30 more minutes of the same Poisson process, so each team's ET
# rate is its effective per-90 rate x (30/90). Real ET scoring behavior may
# differ (tired legs, caution), but quantifying that would be a hand-guess;
# linear time-scaling is the guess-free default (Poisson is memoryless).
ET_MINUTES = 30
# Penalty shootouts are empirically near coin flips — only mildly
# skill-influenced. Any tilt we invented would be an ungrounded number,
# so the continuation resolves unbroken ties at exactly 50/50.
PENALTY_HOME_WIN_P = 0.5

# --- Red-card effect (Piece 3) ----------------------------------------------
# Vecer, Kopriva & Ichiba (2009), "Estimating the Effect of the Red Card in
# Soccer", J. Quantitative Analysis in Sports 5(1): estimated from in-play
# betting data on FIFA World Cup 2006 + Euro 2008 (same competition class as
# ours). The sanctioned team's scoring intensity falls to ~2/3 of its
# original rate; the opponent's rises by a factor of ~5/4. Direction
# corroborated by van Ours & van Tuijl (2017, Empirical Economics) on WC
# matches 1998-2014. Replaces the previous hand-guessed 0.70 / 1.15.
RED_CARD_OWN_MULT = 0.67   # carded team's rate: x ~2/3
RED_CARD_OPP_MULT = 1.25   # opponent's rate:    x ~5/4

# --- Half-time split -------------------------------------------------------
# Goals skew to the second half in tournament football — accumulated fatigue,
# games opening up, substitutions, and second-half stoppage time. Across World
# Cups roughly ~54-56% of goals come after the break, so the match goal rate is
# split unevenly between the halves rather than a flat 50/50. (A flat split
# makes the two halves statistically identical, which is misleading.)
SECOND_HALF_GOAL_SHARE = 0.54


class MatchSimulator:
    def __init__(self, n_simulations: int | None = None, seed: int | None = None,
                 dispersion_cv: float | None = None):
        self.n = n_simulations or config.N_SIMULATIONS
        self.rng = np.random.default_rng(seed)
        # PER-LEAGUE dispersion. None = the WC26 default from config, so the
        # frozen archive replays bit-for-bit; MLS passes its own measured
        # value (see config.MLS_GOAL_DISPERSION_CV).
        self.dispersion_cv = dispersion_cv

    def _dispersed(self, lam_home, lam_away):
        """Gamma-mix the per-sim goal rates (negative binomial dispersion):
        real performances are streakier than pure Poisson — see
        GOAL_DISPERSION_CV in config for the sourcing. Mean-1 multipliers,
        so team strength (the mean) is untouched; only the spread widens.

        NOTE the shape cost: widening the spread inflates P(0 goals) for
        EACH side, which suppresses BTTS and the overs. That is right for
        WC26's sourcing but was measured WRONG for MLS (audit Jul 25 — the
        deployed props ran 4-10pp under actual), hence the per-league
        override rather than a global change."""
        cv = (config.GOAL_DISPERSION_CV if self.dispersion_cv is None
              else self.dispersion_cv)
        if cv <= 0:
            return lam_home, lam_away
        k = 1.0 / (cv * cv)
        return (lam_home * self.rng.gamma(k, 1.0 / k, self.n),
                lam_away * self.rng.gamma(k, 1.0 / k, self.n))

    def simulate(self, home_raw: dict, away_raw: dict, stage: str = "group") -> dict:
        xg_home, xg_away = predict_xg(home_raw, away_raw)

        # --- Red card adjustment: sample card events, penalize the carded side.
        p_red_home = home_raw.get("red_card_risk", 0.06)
        p_red_away = away_raw.get("red_card_risk", 0.06)
        red_home = self.rng.random(self.n) < p_red_home
        red_away = self.rng.random(self.n) < p_red_away

        lam_home = np.full(self.n, xg_home)
        lam_away = np.full(self.n, xg_away)
        # Knockout football is cagier than raw team ratings imply: teams
        # protect leads and avoid risks with elimination on the line. The
        # DIRECTION is well documented (group stages out-score knockouts,
        # e.g. WC2018 group 2.54 goals/match vs lower knockout 90' averages);
        # the exact magnitude is an estimate, tunable via KNOCKOUT_DAMPING.
        if stage == "knockout":
            lam_home *= config.KNOCKOUT_DAMPING
            lam_away *= config.KNOCKOUT_DAMPING
        # performance variance beyond Poisson (see _dispersed)
        lam_home, lam_away = self._dispersed(lam_home, lam_away)
        # Red card: sourced coefficients (see RED_CARD_* constants above)
        lam_home = np.where(red_home, lam_home * RED_CARD_OWN_MULT, lam_home)
        lam_away = np.where(red_home, lam_away * RED_CARD_OPP_MULT, lam_away)
        lam_away = np.where(red_away, lam_away * RED_CARD_OWN_MULT, lam_away)
        lam_home = np.where(red_away, lam_home * RED_CARD_OPP_MULT, lam_home)

        goals_home = self.rng.poisson(lam_home)
        goals_away = self.rng.poisson(lam_away)

        result = self._aggregate_outcomes(goals_home, goals_away, stage,
                                          xg_home, xg_away,
                                          et_lam_home=lam_home,
                                          et_lam_away=lam_away,
                                          fg_lam_home=lam_home,
                                          fg_lam_away=lam_away)
        result["halves"] = self._half_summaries(lam_home, lam_away)
        return result

    # ------------------------------------------------------------------
    def _half_summaries(self, lam_home, lam_away) -> dict:
        """First- and second-half outlooks. Reporting the single most-likely
        HALF scoreline is uninformative — a 45-minute half is almost always
        0-0 for every match — so each half is summarized by numbers that
        actually vary: the result lean (W/D/L), expected goals, and the chance
        of at least one goal. The match rate is split by SECOND_HALF_GOAL_SHARE
        (goals skew after the break) rather than 50/50, so the two halves
        differ realistically instead of coming out identical."""
        out = {}
        for key, share in (("first_half", 1.0 - SECOND_HALF_GOAL_SHARE),
                           ("second_half", SECOND_HALF_GOAL_SHARE)):
            h = self.rng.poisson(np.asarray(lam_home) * share)
            a = self.rng.poisson(np.asarray(lam_away) * share)
            total = h + a
            out[key] = {
                "home_win": round(float(np.mean(h > a)), 4),
                "draw": round(float(np.mean(h == a)), 4),
                "away_win": round(float(np.mean(h < a)), 4),
                "exp_goals": round(float(np.mean(total)), 2),
                "goal_pct": round(float(np.mean(total >= 1)), 4),
            }
        return out

    # ------------------------------------------------------------------
    def _aggregate_outcomes(self, goals_home: np.ndarray, goals_away: np.ndarray,
                            stage: str, xg_home: float, xg_away: float,
                            et_lam_home=None, et_lam_away=None,
                            fg_lam_home=None, fg_lam_away=None) -> dict:
        """Shared tail of every simulation: per-sim FINAL scores in, the
        outcome/props/scorelines/confidence dict out.

        Used by both the pre-match simulate() and the live
        simulate_remaining() so their return shapes structurally cannot
        drift apart — prob_for_outcome_key() works on either result.

        TWO separate latent-rate inputs (they coincide pre-match but
        DIVERGE in play — conflating them was V7 evaluation F3, which froze
        live no-goal at its pre-match value all match long):
          et_lam_*: per-90 rates for the extra-time continuation scale.
          fg_lam_*: rates for the interval the first-goal race actually
                    covers — full match pre-kickoff, the REMAINING
                    interval in play, omitted once a goal has scored.
        Legacy note: the old single lam90_* parameter was each team's per-90 rate
        (scalar, or a per-sim array so red-card handicaps carry into the
        continuation). When provided for a knockout, level regulations
        continue into a simulated ET + penalties (Piece 2); otherwise
        advancement falls back to the half-the-draws approximation.
        """
        # --- Aggregate outcomes
        p_home = float(np.mean(goals_home > goals_away))
        p_draw = float(np.mean(goals_home == goals_away))
        p_away = float(np.mean(goals_home < goals_away))

        total = goals_home + goals_away
        p_btts = float(np.mean((goals_home > 0) & (goals_away > 0)))
        margin = goals_home - goals_away

        props = {"btts": round(p_btts, 4)}
        # Full totals ladder (matches Kalshi's KXWCTOTAL markets, over 0.5-5.5)
        for line in range(6):
            p_over = float(np.mean(total > line + 0.5))
            props[f"over_{line}_5"] = round(p_over, 4)
            props[f"under_{line}_5"] = round(1 - p_over, 4)
        # First-goal race (prices Kalshi KXWCFTTS: "team records the first
        # goal" / "no goal"): independent Poisson processes — P(team first)
        # = (lam_t/lam_tot)·P(any goal), computed PER LATENT-RATE DRAW and
        # then averaged. The old mean-rate shortcut exp(-mean(λ)) discarded
        # the gamma mixture's extra zero mass (E[exp(-λ)] > exp(-E[λ]),
        # Jensen), understating no_goal on every FTTS book. The three
        # outcomes sum to one by construction, per draw and in the mean.
        if fg_lam_home is not None and fg_lam_away is not None:
            lh = np.asarray(fg_lam_home, dtype=float)
            la = np.asarray(fg_lam_away, dtype=float)
            lt = lh + la
            if float(np.mean(lt)) > 0:
                safe_lt = np.where(lt > 0, lt, 1.0)
                p_any_i = 1.0 - np.exp(-lt)
                props["home_first_goal"] = round(
                    float(np.mean(np.where(lt > 0, lh / safe_lt * p_any_i,
                                           0.0))), 4)
                props["away_first_goal"] = round(
                    float(np.mean(np.where(lt > 0, la / safe_lt * p_any_i,
                                           0.0))), 4)
                props["no_goal"] = round(float(np.mean(np.exp(-lt))), 4)

        # Winning margins (matches Kalshi's KXWCSPREAD markets, "wins by 1.5+/2.5+")
        for m_line in (2, 3):
            props[f"home_margin_{m_line}"] = round(float(np.mean(margin >= m_line)), 4)
            props[f"away_margin_{m_line}"] = round(float(np.mean(-margin >= m_line)), 4)

        # Per-team totals (Kalshi TEAMTOTAL) — computed from the FULL goal
        # arrays. The V8 evaluation reproduced a ~2pp systematic
        # understatement (worse at high xG) when these were summed from the
        # truncated top-30 scoreline display list; marginals must never be
        # derived from a display subset.
        for line in range(3):
            props[f"home_team_over_{line}_5"] = round(
                float(np.mean(goals_home > line)), 4)
            props[f"away_team_over_{line}_5"] = round(
                float(np.mean(goals_away > line)), 4)

        # --- Scoreline distribution (top 30 — deep enough that every Kalshi
        # exact-score contract finds its probability; the UI shows fewer)
        pairs, counts = np.unique(
            np.stack([goals_home, goals_away], axis=1), axis=0, return_counts=True
        )
        order = np.argsort(-counts)
        scorelines = [
            {"score": f"{int(h)}-{int(a)}", "prob": round(float(c) / self.n, 4)}
            for (h, a), c in zip(pairs[order][:30], counts[order][:30])
        ]

        # --- Confidence: how decisive is the distribution?
        # Low entropy over {home, draw, away} + tight scoreline mass = confident.
        probs = np.clip(np.array([p_home, p_draw, p_away]), 1e-9, 1)
        entropy = float(-(probs * np.log(probs)).sum())          # 0 .. ln(3)
        confidence = 1.0 - entropy / np.log(3)                    # 0 .. 1
        confidence = 0.5 + 0.5 * confidence                       # floor at 0.5
        confidence /= stage_uncertainty(stage)                    # knockout haircut

        # --- Knockout advancement (Piece 2): real ET + penalties continuation.
        # Regulation W/D/L, props, and scorelines above stay REGULATION-final
        # (that's what the Kalshi market families settle on); only "who
        # advances" continues past 90 minutes.
        if stage == "knockout" and et_lam_home is not None and et_lam_away is not None:
            level = goals_home == goals_away
            et_scale = ET_MINUTES / 90.0
            et_home = self.rng.poisson(np.asarray(et_lam_home) * et_scale, self.n)
            et_away = self.rng.poisson(np.asarray(et_lam_away) * et_scale, self.n)
            still_level = level & (et_home == et_away)
            pens_home = self.rng.random(self.n) < PENALTY_HOME_WIN_P
            home_adv = (goals_home > goals_away) \
                | (level & (et_home > et_away)) \
                | (still_level & pens_home)
            advance = {
                "home": round(float(np.mean(home_adv)), 4),
                "away": round(float(np.mean(~home_adv)), 4),
                "p_reach_et": round(float(np.mean(level)), 4),
                "p_reach_pens": round(float(np.mean(still_level)), 4),
                # Method-of-victory breakdown: prices Kalshi's KXWCMOV ET/PEN
                # contracts ("X wins in extra time" / "X wins on penalties").
                "home_win_et": round(float(np.mean(level & (et_home > et_away))), 4),
                "away_win_et": round(float(np.mean(level & (et_home < et_away))), 4),
                "home_win_pens": round(float(np.mean(still_level & pens_home)), 4),
                "away_win_pens": round(float(np.mean(still_level & ~pens_home)), 4),
                "method": "simulated_et_pens",
            }
        else:
            advance = {
                "home": round(p_home + 0.5 * p_draw, 4),
                "away": round(p_away + 0.5 * p_draw, 4),
                "p_reach_et": round(p_draw, 4),
                "p_reach_pens": None,
                "method": "half_draw_approx",
            }

        return {
            "model_version": MODEL_VERSION,
            "n_simulations": self.n,
            "xg": {"home": xg_home, "away": xg_away},
            "outcomes": {
                "home_win": round(p_home, 4),
                "draw": round(p_draw, 4),
                "away_win": round(p_away, 4),
            },
            "advance": advance,
            "props": props,
            "scorelines": scorelines,
            "confidence": round(float(confidence), 4),
        }

    # ------------------------------------------------------------------
    def simulate_remaining(self, home_raw: dict, away_raw: dict,
                           current_home: int, current_away: int,
                           minutes_elapsed: float, stage: str = "group",
                           red_home: int = 0, red_away: int = 0,
                           phase: str = "auto") -> dict:
        """Live in-play core (Piece 1): simulate only the REMAINDER of a
        match from a known state, seeded with the current score.

        Answers "who wins from HERE?" instead of the pre-match "who wins
        from 0-0?". Every input is checkable state, not a guess:
          - Goal rates come from the same xG model, time-scaled by
            (90 - minutes_elapsed) / 90. A Poisson process is memoryless,
            so the remaining-interval rate scales linearly with time left.
          - The current score seeds every simulation; remaining sampled
            goals are added on top. Totals/BTTS/scorelines therefore
            reflect FINAL totals automatically (e.g. at 1-0, over_0_5 is
            exactly 1.0 and btts is exactly P(away scores in remainder)).
          - Red cards are KNOWN inputs (counts, 0-3 per side), not sampled
            risks. Coefficients are literature-sourced (Vecer et al. 2009,
            WC 2006 + Euro 2008 in-play data: carded side x~2/3, opponent
            x~5/4 — see RED_CARD_* constants); a second red applies the
            same multiplier again (multiplicative extrapolation).
          - `phase` selects the match segment: "auto" infers from the
            minute; "regulation" clamps to 0-90; "et" simulates the
            REMAINING extra time from the current score (minute 90-120)
            then 50/50 penalties; "pens" is the shootout itself (50/50).
            In ET/pens, regulation has already ended LEVEL, so 90-minute
            markets (winner/totals/scores) are settled facts, not
            simulations — only advancement is priced.

        v1 limitations (documented, deliberate):
          - Stoppage time is not modeled (regulation phase clamps at 90).
          - The knockout x0.85 goal damping is inherited from the
            pre-match model (known hand-tuned debt) for consistency.

        Returns the exact same shape as simulate(), plus a "live_state"
        block, so prob_for_outcome_key() works on either result unchanged.
        """
        if minutes_elapsed < 0:
            raise ValueError("minutes_elapsed cannot be negative")
        if current_home < 0 or current_away < 0:
            raise ValueError("current score cannot be negative")
        red_home, red_away = int(red_home), int(red_away)

        if phase == "auto":
            phase = ("et" if stage == "knockout" and minutes_elapsed > 90
                     else "regulation")

        xg_home, xg_away = predict_xg(home_raw, away_raw)

        # Effective per-90 rates: damping + KNOWN cards applied BEFORE time
        # scaling — the ET continuation reuses these at 30/90. Card counts
        # apply the sourced multiplier once per red (0.67^n / 1.25^n).
        rate_home, rate_away = xg_home, xg_away
        if stage == "knockout":          # same damping as pre-match
            rate_home *= config.KNOCKOUT_DAMPING
            rate_away *= config.KNOCKOUT_DAMPING
        rate_home *= (RED_CARD_OWN_MULT ** red_home) * (RED_CARD_OPP_MULT ** red_away)
        rate_away *= (RED_CARD_OWN_MULT ** red_away) * (RED_CARD_OPP_MULT ** red_home)

        if phase in ("et", "pens"):
            return self._continuation_phase(
                phase, rate_home, rate_away, current_home, current_away,
                minutes_elapsed, xg_home, xg_away, red_home, red_away)

        minutes_elapsed = min(float(minutes_elapsed), 90.0)
        frac_remaining = max(0.0, (90.0 - float(minutes_elapsed)) / 90.0)
        lam_home = np.full(self.n, rate_home * frac_remaining)
        lam_away = np.full(self.n, rate_away * frac_remaining)
        # same performance variance in the remainder as pre-match — exact
        # scores settle on the 90, so the live cluster needs it too
        lam_home, lam_away = self._dispersed(lam_home, lam_away)

        rem_home = self.rng.poisson(lam_home, self.n)
        rem_away = self.rng.poisson(lam_away, self.n)
        goals_home = current_home + rem_home
        goals_away = current_away + rem_away

        goalless = (current_home + current_away) == 0
        result = self._aggregate_outcomes(
            goals_home, goals_away, stage, xg_home, xg_away,
            et_lam_home=rate_home, et_lam_away=rate_away,
            # first-goal race: the remaining interval's dispersed rates —
            # passing the full-match rates froze no_goal at its pre-match
            # value through minute 89 (V7 evaluation F3). Once a goal has
            # scored the race is settled: no first-goal props at all.
            fg_lam_home=lam_home if goalless else None,
            fg_lam_away=lam_away if goalless else None)
        result["live_state"] = {
            "score": f"{current_home}-{current_away}",
            "minutes_elapsed": round(float(minutes_elapsed), 1),
            "minutes_remaining": round(90.0 * frac_remaining, 1),
            "phase": "regulation",
            "red_home": red_home,
            "red_away": red_away,
            # report the MEAN remaining rate (lam arrays carry per-sim
            # dispersion multipliers; the echo is the expectation)
            "lambda_remaining": {"home": round(rate_home * frac_remaining, 3),
                                 "away": round(rate_away * frac_remaining, 3)},
        }
        return result

    # ------------------------------------------------------------------
    def _continuation_phase(self, phase: str, rate_home: float,
                            rate_away: float, current_home: int,
                            current_away: int, minutes_elapsed: float,
                            xg_home: float, xg_away: float,
                            red_home: int, red_away: int) -> dict:
        """Live state INSIDE extra time or at penalties. Regulation ended
        level (that's how the match got here), so the 90-minute outcomes are
        settled facts: draw = 1.0, and totals/exact-scores are NOT priced
        (the 90' score can't be recovered from the current ET score, and
        those books have settled anyway). What's simulated is advancement:
        the remaining ET minutes at the time-scaled rates, then 50/50 pens.
        The shootout itself is a flat coin flip — anything else would be an
        invented number."""
        if phase == "pens":
            p_home = PENALTY_HOME_WIN_P
            advance = {
                "home": round(p_home, 4), "away": round(1 - p_home, 4),
                "p_reach_et": 1.0, "p_reach_pens": 1.0,
                "home_win_et": 0.0, "away_win_et": 0.0,
                "home_win_pens": round(p_home, 4),
                "away_win_pens": round(1 - p_home, 4),
                "method": "penalty_coinflip",
            }
            minutes_remaining = 0.0
            lam_home = lam_away = 0.0
        else:
            minute = min(max(float(minutes_elapsed), 90.0), 120.0)
            minutes_remaining = 120.0 - minute
            lam_home = rate_home * minutes_remaining / 90.0
            lam_away = rate_away * minutes_remaining / 90.0
            # same performance variance in the ET remainder as regulation
            # and pre-match — advancement rides these tails too (the echo
            # below stays the scalar expectation)
            lam_h, lam_a = self._dispersed(np.full(self.n, lam_home),
                                           np.full(self.n, lam_away))
            et_home = current_home + self.rng.poisson(lam_h, self.n)
            et_away = current_away + self.rng.poisson(lam_a, self.n)
            level = et_home == et_away
            pens_home = self.rng.random(self.n) < PENALTY_HOME_WIN_P
            home_adv = (et_home > et_away) | (level & pens_home)
            advance = {
                "home": round(float(np.mean(home_adv)), 4),
                "away": round(float(np.mean(~home_adv)), 4),
                "p_reach_et": 1.0,
                "p_reach_pens": round(float(np.mean(level)), 4),
                "home_win_et": round(float(np.mean(et_home > et_away)), 4),
                "away_win_et": round(float(np.mean(et_home < et_away)), 4),
                "home_win_pens": round(float(np.mean(level & pens_home)), 4),
                "away_win_pens": round(float(np.mean(level & ~pens_home)), 4),
                "method": "simulated_et_pens",
            }
        # confidence: how decisive is the advancement call? (binary entropy)
        p = min(max(advance["home"], 1e-9), 1 - 1e-9)
        entropy = -(p * np.log(p) + (1 - p) * np.log(1 - p))
        confidence = 0.5 + 0.5 * (1.0 - float(entropy) / np.log(2))
        return {
            "model_version": MODEL_VERSION,
            "n_simulations": self.n,
            "xg": {"home": xg_home, "away": xg_away},
            # regulation ended level — a settled fact, not a simulation
            "outcomes": {"home_win": 0.0, "draw": 1.0, "away_win": 0.0},
            "advance": advance,
            "props": {},          # 90-min props settled; nothing to price
            "scorelines": [],
            "confidence": round(confidence, 4),
            "live_state": {
                "score": f"{current_home}-{current_away}",
                "minutes_elapsed": round(float(minutes_elapsed), 1),
                "minutes_remaining": round(minutes_remaining, 1),
                "phase": phase,
                "red_home": red_home,
                "red_away": red_away,
                "lambda_remaining": {"home": round(lam_home, 3),
                                     "away": round(lam_away, 3)},
            },
        }

    # ------------------------------------------------------------------
    def prob_for_outcome_key(self, sim: dict, outcome_key: str) -> float | None:
        """Map a Kalshi market outcome_key to a simulated probability."""
        if outcome_key in sim["outcomes"]:
            return sim["outcomes"][outcome_key]
        if outcome_key in sim["props"]:
            return sim["props"][outcome_key]
        # Knockout "to advance" markets. Piece 2: prefer the simulated
        # ET + penalties continuation when present; fall back to the old
        # half-the-draws coin-flip approximation for legacy sim dicts.
        if outcome_key == "home_advance":
            adv = sim.get("advance")
            if adv is not None:
                return adv["home"]
            return round(sim["outcomes"]["home_win"] + 0.5 * sim["outcomes"]["draw"], 4)
        if outcome_key == "away_advance":
            adv = sim.get("advance")
            if adv is not None:
                return adv["away"]
            return round(sim["outcomes"]["away_win"] + 0.5 * sim["outcomes"]["draw"], 4)
        # Method-of-victory continuations (Kalshi KXWCMOV ET/PEN): only priced
        # when the sim ran the real ET+pens continuation — legacy dicts skip.
        if outcome_key in ("home_win_et", "away_win_et",
                           "home_win_pens", "away_win_pens"):
            adv = sim.get("advance") or {}
            return adv.get(outcome_key)
        # Exact final scores: score_2_0 → "2-0" (from Kalshi KXWCSCORE).
        # Only priced when the scoreline appears in our top-10 distribution;
        # rarer scores return None and get skipped rather than mispriced.
        if outcome_key.startswith("score_"):
            parts = outcome_key.split("_")
            if len(parts) == 3:
                target = f"{parts[1]}-{parts[2]}"
                for s in sim["scorelines"]:
                    if s["score"] == target:
                        return s["prob"]
            return None
        # exact scorelines legacy demo keys: home_2_0 -> "2-0"
        if outcome_key.startswith(("home_", "away_")) and outcome_key.count("_") == 2:
            _, h, a = outcome_key.split("_")
            target = f"{h}-{a}"
            for s in sim["scorelines"]:
                if s["score"] == target:
                    return s["prob"]
            return 0.0
        return None
