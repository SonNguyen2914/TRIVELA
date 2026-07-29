"""League-derived xG ratings, and the refusal that keeps them honest.

Son's rule: a club's xG rating comes from the league that club plays in. Each
rating is therefore fitted WITHIN one competition-season, using this
platform's existing rating construction — recency weighting plus partial
pooling toward the league mean, the same shape as `model_mls.fit`'s xG arm and
the ladder's M3 rung — and carries the competition it was fitted in, the number
of fixtures behind it, the fitted window, and which provider produced it.

THE CENTRAL CONSTRAINT
======================

Two ratings from different competitions MUST NEVER BE ARITHMETICALLY
COMBINED. A J1 League rating and a Bundesliga rating are fitted on different
populations, against different league means, from different sample sizes. Each
is a statement about a club RELATIVE TO ITS OWN LEAGUE and nothing else. The
number 1.20 means "20% above this league's average xG"; two leagues' 1.20s are
not the same quantity, and subtracting them produces a figure with no
referent whatsoever.

That is not a stylistic preference. A friendlies "prediction" assembled from
two incomparable ratings would be exactly the fabricated-confidence defect
this repository exists to avoid: it would look like a forecast, carry a
plausible number, and mean nothing. `src/friendlies.py` already states that a
forecast is *structurally unavailable* on that surface because no rating
exists for those clubs. This module creates ratings — so it must not quietly
turn that "unavailable" into "available" by making the arithmetic possible.

ENFORCEMENT IS IN THE TYPES, NOT IN A COMMENT
---------------------------------------------

A comment saying "do not subtract these" is worth nothing; the next person to
touch the file writes `home.attack - away.attack` and the code obliges. So the
strengths are not bare floats. `ScopedStrength` carries its competition scope
and its arithmetic operators refuse a different scope:

    >>> dortmund.attack - cerezo.attack
    CrossCompetitionArithmetic: refusing to subtract ...

Ordering is refused too, because "Dortmund's attack is higher than Cerezo's"
is the same unfounded claim as their difference, just spelled with a `>`.
`pair_for_pricing` is the one gate anything resembling a fixture model must
pass, and it refuses a cross-competition pair outright.

Unwrapping to a plain float is possible — `for_display()` — because a number
has to reach a template somehow. That is deliberate and deliberately
conspicuous: it is a single greppable call, named for the only thing it is
for, and it is the seam a reviewer checks. What is NOT possible is doing the
arithmetic by accident.

WHAT THIS MODULE DOES NOT DO
----------------------------

It does not price, predict or forecast anything, and it produces no match
probability. It has no MLS path: MLS xG comes from Sportec (free, measured,
inside the engine signature) and `apifootball` refuses league 253 by name.
Every rating records its `source`, so the two providers can never be pooled
into one population without that being visible in the data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import config
from src.live import apifootball

# Recency half-life is READ from the MLS model rather than copied, exactly as
# `model_eval.LadderSpec` reads its hyperparameters: a copy is free to drift
# from the construction it claims to mirror. model_mls is never modified — it
# is inside the MLS engine signature — only read.
from src.live.model_mls import HALF_LIFE_DAYS

SOURCE_APIFOOTBALL = "api-football"
SOURCE_SPORTEC = "sportec"

# Refusal reasons a club can carry instead of a rating. Each is a DIFFERENT
# truth and gets different words on any surface — never a blank, never a zero.
NO_LEAGUE_LOOKUP = "league_not_looked_up"
LEAGUE_UNRESOLVED = "league_unresolved"
LEAGUE_AMBIGUOUS = "league_ambiguous"
COVERAGE_UNMEASURED = "league_coverage_unmeasured"
COVERAGE_ABSENT = "league_has_no_xg"
NOT_INGESTED = "league_not_ingested"
INSUFFICIENT_SAMPLE = "insufficient_fixtures"


class CrossCompetitionArithmetic(Exception):
    """Raised when code tries to combine or order ratings fitted in different
    competitions. The refusal IS the feature: see the module docstring."""


class CrossCompetitionComparison(CrossCompetitionArithmetic):
    """Ordering two ratings from different competitions — 'A is stronger than
    B' across leagues is the same unfounded claim as their difference."""


@dataclass(frozen=True)
class XgScope:
    """The population a rating was fitted on. Equality of scope is the ONLY
    licence for arithmetic between two strengths.

    `season` is part of identity, not decoration: the same league two seasons
    apart has a different mean, a different set of clubs and a different
    population, so pooling across seasons is the same error as pooling across
    leagues."""
    source: str            # api-football | sportec
    league_id: int
    season: int

    @property
    def key(self) -> str:
        return f"{self.source}:{self.league_id}:{self.season}"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class ScopedStrength:
    """A rating strength that knows which population it came from.

    Arithmetic and ordering are defined WITHIN a scope and refused ACROSS
    scopes. Every operator returns NotImplemented for a non-ScopedStrength
    operand rather than silently accepting a float: `rating.attack - 1.0`
    would be a scope-free number pretending to be a scoped one."""
    scope: XgScope
    value: float

    def _require_same(self, other: "ScopedStrength", op: str) -> None:
        if self.scope != other.scope:
            raise CrossCompetitionArithmetic(
                f"refusing to {op} xG strengths fitted in different "
                f"competitions ({self.scope.key} vs {other.scope.key}). "
                "These ratings are each relative to their OWN league's mean, "
                "so the result would have no referent. Display both; price "
                "from neither.")

    def __sub__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_same(other, "subtract")
        return ScopedStrength(self.scope, self.value - other.value)

    def __add__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_same(other, "add")
        return ScopedStrength(self.scope, self.value + other.value)

    def __mul__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_same(other, "multiply")
        return ScopedStrength(self.scope, self.value * other.value)

    def __truediv__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_same(other, "divide")
        return ScopedStrength(self.scope, self.value / other.value)

    # Ordering: refused across scopes for the same reason as subtraction.
    # 'Dortmund is stronger than Cerezo' is exactly the claim these ratings
    # cannot support, and spelling it with `>` does not make it sound.
    def _require_orderable(self, other: "ScopedStrength") -> None:
        if self.scope != other.scope:
            raise CrossCompetitionComparison(
                f"refusing to order xG strengths from different competitions "
                f"({self.scope.key} vs {other.scope.key}) — 'stronger than' "
                "across leagues is not a claim these ratings can support.")

    def __lt__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_orderable(other)
        return self.value < other.value

    def __gt__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_orderable(other)
        return self.value > other.value

    def __le__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_orderable(other)
        return self.value <= other.value

    def __ge__(self, other):
        if not isinstance(other, ScopedStrength):
            return NotImplemented
        self._require_orderable(other)
        return self.value >= other.value

    def for_display(self) -> float:
        """The bare number, for a template. The ONLY sanctioned way out of the
        scope wrapper, named so that a reviewer can grep every place a scoped
        strength becomes an unscoped float."""
        return self.value


@dataclass(frozen=True)
class LeagueXgRating:
    """One club's xG attack/defence strength, fitted inside ONE league-season.

    `attack` > 1 means the club generates more xG than its league's average
    team; `defence` < 1 means it concedes less. Both are relative to THIS
    league's mean and carry no meaning outside it — which is why they are
    `ScopedStrength` and not floats."""
    scope: XgScope
    provider_team_id: int
    team_name: str
    attack: ScopedStrength
    defence: ScopedStrength
    n_fixtures: int                 # fixtures that actually carried xG
    window_start: datetime | None
    window_end: datetime | None
    league_mean_xg: float
    shrink_games: float
    half_life_days: float

    @property
    def source(self) -> str:
        return self.scope.source

    @property
    def competition_key(self) -> str:
        return self.scope.key

    def as_dict(self) -> dict:
        """Serialization for the API. Values are unwrapped here — once, at the
        boundary — and the payload carries `competition_key` beside them so a
        consumer cannot lose track of which population the number describes.
        `comparable_across_competitions` ships as an explicit False rather
        than being left to a reader's judgement."""
        return {
            "provider_team_id": self.provider_team_id,
            "team_name": self.team_name,
            "attack": round(self.attack.for_display(), 4),
            "defence": round(self.defence.for_display(), 4),
            "n_fixtures": self.n_fixtures,
            "competition_key": self.competition_key,
            "league_id": self.scope.league_id,
            "season": self.scope.season,
            "source": self.source,
            "window_start": (self.window_start.isoformat()
                             if self.window_start else None),
            "window_end": (self.window_end.isoformat()
                           if self.window_end else None),
            "league_mean_xg": round(self.league_mean_xg, 4),
            "shrink_games": self.shrink_games,
            "half_life_days": self.half_life_days,
            "comparable_across_competitions": False,
        }


def pair_for_pricing(home: LeagueXgRating, away: LeagueXgRating):
    """The one gate any fixture-level use of two ratings must pass.

    Refuses a cross-competition pair. This exists so that the refusal is
    reachable by name from anywhere that might one day try to build a fixture
    model out of these ratings — including a caller that never touches a
    `ScopedStrength` directly. On the friendlies surface it always refuses,
    because a friendly between two clubs from the same league-season is the
    exception rather than the rule, and the correct behaviour there is to show
    both ratings and price nothing."""
    if home.scope != away.scope:
        raise CrossCompetitionArithmetic(
            f"refusing to pair xG ratings from different competitions for "
            f"pricing ({home.scope.key} vs {away.scope.key}). A match "
            "probability built from two league-relative ratings fitted on "
            "different populations would be a fabricated number. No forecast "
            "is available for this fixture — which is a fact about the data, "
            "not a gap to fill.")
    return home, away


# --- the fit ---------------------------------------------------------------

def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _weight(days_ago: float, half_life: float) -> float:
    return 0.5 ** (max(days_ago, 0.0) / half_life)


def fit_league_ratings(rows: list[dict], scope: XgScope,
                       as_of: datetime | None = None,
                       shrink_games: float | None = None,
                       half_life: float | None = None,
                       min_fixtures: int | None = None) -> dict:
    """Per-club xG attack/defence for ONE league-season.

    Pure function of its inputs — no database, no network, no clock unless
    `as_of` is omitted — so it is directly testable and a walk-forward caller
    can hand it a prior-only slice.

    Construction mirrors `model_mls.fit`'s xG arm (which the M3 rung measures):
    each club's xG-for and xG-against are recency-weighted with a 90-day
    half-life, divided by the league's own recency-weighted mean xG per
    team-game, then shrunk toward 1.0 by a prior weight expressed in games.
    A fixture whose xG is absent contributes NOTHING — it is not counted as a
    zero, and it does not inflate `n_fixtures`. That is what makes partial
    league coverage degrade gracefully instead of biasing every rating in the
    league downward.

    -> {"scope", "ratings": {team_id: LeagueXgRating}, "league_mean_xg",
        "n_team_matches", "refused": {team_id: reason}}
    """
    as_of = _utc(as_of) or datetime.now(timezone.utc)
    k = (shrink_games if shrink_games is not None
         else config.LEAGUE_XG_SHRINK_GAMES)
    hl = half_life if half_life is not None else HALF_LIFE_DAYS
    need = (min_fixtures if min_fixtures is not None
            else config.LEAGUE_XG_MIN_FIXTURES)

    usable = [r for r in rows
              if r.get("xg") is not None and r.get("xg_against") is not None
              and r.get("kickoff_utc") is not None]
    xgf: dict[int, float] = {}
    xga: dict[int, float] = {}
    wsum: dict[int, float] = {}
    counts: dict[int, int] = {}
    names: dict[int, str] = {}
    first: dict[int, datetime] = {}
    last: dict[int, datetime] = {}
    tot_xg = tot_w = 0.0
    for r in usable:
        tid = r["provider_team_id"]
        ko = _utc(r["kickoff_utc"])
        days = (as_of - ko).total_seconds() / 86400
        w = _weight(days, hl)
        xgf[tid] = xgf.get(tid, 0.0) + w * float(r["xg"])
        xga[tid] = xga.get(tid, 0.0) + w * float(r["xg_against"])
        wsum[tid] = wsum.get(tid, 0.0) + w
        counts[tid] = counts.get(tid, 0) + 1
        if r.get("team_name"):
            names[tid] = r["team_name"]
        if tid not in first or ko < first[tid]:
            first[tid] = ko
        if tid not in last or ko > last[tid]:
            last[tid] = ko
        tot_xg += w * float(r["xg"])
        tot_w += w

    if tot_w <= 0:
        return {"scope": scope, "ratings": {}, "league_mean_xg": None,
                "n_team_matches": 0, "refused": {},
                "reason": NOT_INGESTED}
    league_mean = tot_xg / tot_w
    ratings: dict[int, LeagueXgRating] = {}
    refused: dict[int, str] = {}
    for tid, w in wsum.items():
        if counts[tid] < need:
            # below the sample floor the club is NOT rated. Saying 'not enough
            # fixtures' is the honest answer; a heavily-shrunk number would
            # read as a measurement of the club when it is mostly the prior.
            refused[tid] = INSUFFICIENT_SAMPLE
            continue
        atk = (xgf[tid] / league_mean + k) / (w + k)
        dfc = (xga[tid] / league_mean + k) / (w + k)
        ratings[tid] = LeagueXgRating(
            scope=scope, provider_team_id=tid,
            team_name=names.get(tid, str(tid)),
            attack=ScopedStrength(scope, atk),
            defence=ScopedStrength(scope, dfc),
            n_fixtures=counts[tid],
            window_start=first.get(tid), window_end=last.get(tid),
            league_mean_xg=league_mean, shrink_games=k, half_life_days=hl)
    return {"scope": scope, "ratings": ratings, "league_mean_xg": league_mean,
            "n_team_matches": len(usable), "refused": refused}


def league_ratings(league_id: int, season: int | None = None,
                   as_of: datetime | None = None) -> dict:
    """Fitted ratings for one league, read from the stored xG observations.

    Refuses BEFORE fitting when coverage was measured absent, and says which
    of the several different 'no ratings' truths applies — an unmeasured
    league and a measured-empty one are not the same statement."""
    cov = apifootball.coverage_for(league_id, season)
    if cov is None:
        return {"league_id": league_id, "season": season, "ratings": {},
                "refused_reason": COVERAGE_UNMEASURED, "coverage": None}
    if cov["verdict"] == apifootball.XG_ABSENT:
        return {"league_id": league_id, "season": cov["season"],
                "ratings": {}, "refused_reason": COVERAGE_ABSENT,
                "coverage": cov}
    if not cov["fittable"]:
        return {"league_id": league_id, "season": cov["season"],
                "ratings": {}, "refused_reason": COVERAGE_UNMEASURED,
                "coverage": cov}
    use_season = season if season is not None else cov["season"]
    rows = apifootball.league_xg_rows(league_id, use_season)
    if not rows:
        return {"league_id": league_id, "season": use_season, "ratings": {},
                "refused_reason": NOT_INGESTED, "coverage": cov}
    scope = XgScope(source=SOURCE_APIFOOTBALL, league_id=league_id,
                    season=use_season)
    fit = fit_league_ratings(rows, scope, as_of=as_of)
    return {"league_id": league_id, "season": use_season,
            "scope": scope.key, "ratings": fit["ratings"],
            "refused": fit["refused"],
            "league_mean_xg": fit["league_mean_xg"],
            "n_team_matches": fit["n_team_matches"],
            "refused_reason": None if fit["ratings"] else NOT_INGESTED,
            "coverage": cov}


# --- the club-facing surface ----------------------------------------------

# Deliberately not a docstring on a function: this string is served with every
# response so no consumer can render the ratings without the constraint. The
# wording refuses forecast vocabulary for the same reason
# `friendlies.IMPLIED_ATTRIBUTION` does.
RATINGS_ATTRIBUTION = (
    "League-derived xG form: each club's expected-goals strength relative to "
    "its OWN league's average, fitted only on that league's fixtures. Two "
    "clubs' ratings come from different leagues and are NOT comparable to "
    "each other — no match probability is or can be derived from them.")

NOT_COMPARABLE_NOTE = (
    "Ratings from different leagues are fitted on different populations and "
    "different scales. They are shown side by side for context only.")


def _refusal(club: str, reason: str, detail: dict | None = None) -> dict:
    return {"club": club, "rated": False, "reason": reason,
            "rating": None, **(detail or {})}


def club_rating(club_name: str, as_of: datetime | None = None,
                _cache: dict | None = None) -> dict:
    """One club's league-derived xG rating, or an explicit refusal.

    Never returns a blank or a zero. The refusal reasons are distinct because
    they are distinct facts:
      league_not_looked_up        we have not resolved this club yet
      league_unresolved           the provider had no unambiguous club/league
      league_ambiguous            more than one candidate — never guessed
      league_coverage_unmeasured  we have not measured that league's xG
      league_has_no_xg            MEASURED: the provider has no xG there
      league_not_ingested         coverage is fine, the fixtures are not held
      insufficient_fixtures       held, but below the sample floor
    """
    cl = apifootball.club_league(club_name)
    if cl is None:
        return _refusal(club_name, NO_LEAGUE_LOOKUP)
    if cl["resolution"] in ("ambiguous_team", "ambiguous_league"):
        return _refusal(club_name, LEAGUE_AMBIGUOUS,
                        {"resolution": cl["resolution"]})
    if cl["resolution"] != "resolved" or not cl.get("league_id"):
        return _refusal(club_name, LEAGUE_UNRESOLVED,
                        {"resolution": cl["resolution"]})
    league = {"league_id": cl["league_id"], "season": cl["season"],
              "league_name": cl["league_name"],
              "league_country": cl["league_country"]}
    key = (cl["league_id"], cl["season"])
    if _cache is not None and key in _cache:
        lr = _cache[key]
    else:
        lr = league_ratings(cl["league_id"], cl["season"], as_of=as_of)
        if _cache is not None:
            _cache[key] = lr
    if lr.get("refused_reason") and not lr.get("ratings"):
        return _refusal(club_name, lr["refused_reason"], {"league": league,
                        "coverage": lr.get("coverage")})
    rating = (lr.get("ratings") or {}).get(cl["provider_team_id"])
    if rating is None:
        reason = ((lr.get("refused") or {}).get(cl["provider_team_id"])
                  or INSUFFICIENT_SAMPLE)
        return _refusal(club_name, reason, {"league": league,
                        "coverage": lr.get("coverage")})
    return {"club": club_name, "rated": True, "reason": None,
            "league": league, "coverage": lr.get("coverage"),
            "rating": rating.as_dict()}


def fixture_ratings(home_name: str, away_name: str,
                    as_of: datetime | None = None,
                    _cache: dict | None = None) -> dict:
    """Both clubs' league-derived ratings for one fixture, with the
    comparability question answered explicitly rather than left open.

    `forecast` is always None and `forecast_available` always False. Where the
    two clubs happen to share a league-season, `same_competition` is True and
    the pair would survive `pair_for_pricing` — but this surface still
    publishes no probability, because a friendly's rotation and substitution
    behaviour is what makes it worthless as forecast evidence, and that
    objection is untouched by the ratings existing. The field is reported so
    the distinction is visible, not so it can be acted on."""
    cache = _cache if _cache is not None else {}
    home = club_rating(home_name, as_of=as_of, _cache=cache)
    away = club_rating(away_name, as_of=as_of, _cache=cache)
    same = bool(
        home["rated"] and away["rated"]
        and home["rating"]["competition_key"] == away["rating"]["competition_key"])
    return {
        "home": home, "away": away,
        "same_competition": same,
        "comparable": False,
        "not_comparable_note": NOT_COMPARABLE_NOTE,
        "forecast": None,
        "forecast_available": False,
        "forecast_unavailable_reason": (
            "No match probability is derived from these ratings. Each is "
            "relative to its own league's average, so combining two of them "
            "would produce a number with no referent — and friendlies are "
            "structurally worthless as forecast evidence regardless."),
        "attribution": RATINGS_ATTRIBUTION,
    }


def summary() -> dict:
    """What this surface can and cannot say right now: the measured coverage
    table, the ingest census and any retroactive provider corrections."""
    return {
        "coverage": apifootball.coverage_table(),
        "ingest": apifootball.ingest_summary(),
        "retroactive_corrections": apifootball.retroactive_corrections(),
        "clubs": apifootball.club_league_table(),
        "parameters": {
            "shrink_games": config.LEAGUE_XG_SHRINK_GAMES,
            "half_life_days": HALF_LIFE_DAYS,
            "min_fixtures": config.LEAGUE_XG_MIN_FIXTURES,
            "provenance": (
                "shrink and half-life are STARTING POINTS carried from MLS "
                "league play (model_mls), NOT swept on these leagues' own "
                "data. Nothing prices off these ratings."),
        },
        "attribution": RATINGS_ATTRIBUTION,
        "mls_note": (
            "MLS is served by Sportec, not API-Football: its xG already "
            "exists free with a measured effect and sits inside the MLS "
            "engine signature. apifootball refuses league 253 by name."),
    }
