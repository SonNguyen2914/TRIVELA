"""Per-team playstyle VECTORS, fitted inside one league-season.

WHAT THIS IS. A style vector is a team's position on a small number of
CONTINUOUS axes, each fitted from that team's own league fixtures with the
same recency-weighting and partial-pooling discipline `xg_ratings` uses for
attack/defence. It is a description, not a forecast: nothing in this module
produces a probability, and nothing prices off it. The pre-registered rung
that would test whether style carries information beyond rating difference is
`strategy/BACKLOG.md` S-10, and it is deliberately UNFITTED here.

THE FIVE AXES ARE PRE-REGISTERED, NOT DISCOVERED
================================================

They are frozen in `AXES` below, chosen from a 40-fixture EPL 2025 look
(2026-07-29) BEFORE any full-season fit was run, and they must not be edited
in response to a fit that disappoints. An axis table amended to contain
exactly what the last run needed is the definition of tuning the instrument to
the data — see `research_archive/apifootball_tuning_incident_2026-07-29.json`
for what that costs. A new axis is a NEW HYPOTHESIS owed its own registration,
which is why `CANDIDATE_AXES` is a separate table: the sixth candidate (a
team's OWN offsides, as a verticality proxy) is measured alongside the five
without being one of them.

WHY FIVE AND NOT ONE. The axes are demonstrably not collinear, and the
evidence is worth keeping in the source because it is the entire justification
for carrying five numbers rather than one. From that 40-fixture EPL look:

  - Leeds and Forest both sit near 41-44% possession, yet Leeds generates
    0.156 xG per shot against Forest's 0.068 — a 2.3x gap in chance quality
    between two teams a possession-only description would call identical.
  - West Ham at 41% possession has a HIGHER inside-shot share (0.78) than
    Manchester City at 75% (0.75).

WHAT THE FULL SEASON THEN SAID (EPL/La Liga 380 fixtures each, Liga MX 337,
measured 2026-07-29 and archived as `team_style_separation_and_correlation_
2026-07-29.json`). The pilot figures above are LEFT AS WRITTEN because they
are what was registered; this paragraph records how they held up, which is a
different thing from editing them.

  - The GENERAL claim held, and more strongly than the anecdotes did:
    ball_dominance x shot_location came back r = -0.02 in the EPL over a full
    season. Possession and how a team creates are very nearly orthogonal
    there. (+0.16 La Liga, -0.32 Liga MX.)
  - Anecdote 2 SURVIVED almost unchanged: West Ham 42.4% possession / 0.741
    inside share vs Manchester City 61.7% / 0.709.
  - Anecdote 1's MAGNITUDE DID NOT. Over the full season Leeds and Forest are
    still nearly identical on possession (45.8 vs 45.0) but their chance
    quality is 0.114 vs 0.101 — a 1.13x gap, not 2.3x. A 40-fixture sample of
    a 380-fixture season gives each team about four matches, so per-team
    extremes in the pilot were substantially noise, and EVERY axis's pilot
    spread was wider than its full-season spread (possession 33.5 -> 20.4,
    xG/shot 0.096 -> 0.056, GK saves 3.9 -> 1.9). The pilot overstated
    separation by roughly 1.3x-2x per axis and named a different extreme team
    on four of the five.

The lesson is kept in the source deliberately: a small sample flatters an
axis, so `separation_report` reports the n behind every figure and
`correlation_report` reports the team count behind every coefficient. Where
two axes correlate above `COLLINEAR_ABS_R` in a league, that report says so
and flags that they may be one axis there — a flag to report, never a licence
to merge.

COORDINATES ARE THE REPRESENTATION. LABELS ARE A RENDERING.
==========================================================

Son asked whether sub-styles would help. The answer is no: more labels divide
the same evidence, and this feed cannot support what sub-styles would name. So
the continuous vector is what gets stored and computed on, and a human-readable
label is DERIVED from it for display only.

"For display only" is a structural property here, not a comment:

  - `StyleVector` has NO label field. There is nothing to read. A caller that
    wants a label must call `render_label(vector)`, which is a free function.
  - `render_label` returns a `DisplayLabel`, whose every arithmetic,
    comparison, hashing, truthiness, iteration and numeric-coercion dunder
    RAISES `LabelIsNotAnInput`. You cannot branch on it (`if label:`), compare
    it (`label == "..."`), key a dict with it, bucket by it, or feed it to a
    model. The only ways out are `str()` and `for_display()` — one greppable
    seam, named for the only thing it is for.
  - `style_covariates`, the one function that turns vectors into model inputs,
    iterates ONLY the frozen axis registry and refuses a `DisplayLabel`
    outright.

So a future author who tries to fit on the label gets an exception, not a
plausible number. That is the same bet `ScopedStrength` makes: a comment
saying "do not do this" is worth nothing, because the next person to touch the
file writes it anyway and the code obliges.

THE CROSS-COMPETITION REFUSAL APPLIES TO STYLE, FOR THE SAME REASON
===================================================================

These axes are LEAGUE-RELATIVE. 55% possession is a different fact in the
Championship than in La Liga; a z-score against one league's distribution has
no referent in another. So this module does not invent a parallel scope
mechanism — it reuses `xg_ratings.XgScope` and `xg_ratings.ScopedStrength`
unchanged, and every axis coordinate is a `ScopedStrength`. A Bundesliga style
vector therefore CANNOT be differenced against a J-League one: the operators
raise `CrossCompetitionArithmetic`. `pair_for_style_model` is the named gate
any fixture-level use must pass.

(`XgScope`'s name is xG-historical; its content is `source + league_id +
season`, which is exactly "the population this was fitted on". Reusing it is
the point — a second scope type would let a style vector and an xG rating
disagree about what a competition is.)

ABSENT IS NEVER ZERO
====================

API-Football ships a statistic type with a null value routinely. A null read
as 0 would make a team look like it never won possession, never drew an
offside and never forced a save. `parse_count` and `parse_percent` return None
for null / '' / '-' / non-numeric alike, a fixture missing an axis contributes
NOTHING to that axis (it does not count toward `n_obs` and does not drag the
mean), and an axis with no usable observations is REFUSED by name rather than
returned as 0.0.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from src.live import apifootball
from src.live.db import get_session, plane_ready

# Recency half-life is READ from the MLS model rather than copied, exactly as
# `xg_ratings` and `model_eval.LadderSpec` read theirs: a copy is free to drift
# from the construction it claims to mirror. model_mls is never modified — it
# is inside the MLS engine signature — only read.
from src.live.model_mls import HALF_LIFE_DAYS
from src.live.xg_ratings import (SOURCE_APIFOOTBALL, SOURCE_SPORTEC,
                                 CrossCompetitionArithmetic, ScopedStrength,
                                 XgScope)

# --- axis registry: PRE-REGISTERED, frozen -------------------------------

# `kind`:
#   "rate"  aggregated as a ratio of recency-weighted sums (numerator and
#           denominator accumulated separately, then divided). Aggregating
#           per-fixture ratios instead would let a 1-shot match count as
#           much as a 25-shot one.
#   "level" aggregated as a recency-weighted mean per team-game.
AXES: dict[str, dict] = {
    "ball_dominance": {
        "kind": "level",
        "field": "possession_pct",
        "human": "ball dominance",
        "means": "share of possession, percent",
        "high": "dominates the ball", "low": "cedes the ball",
        "measured_range": "68.3 (Man City) -> 34.8 (Wolves), EPL 2025 n=40",
    },
    "shot_location": {
        "kind": "rate",
        "numerator": "shots_inside_box", "denominator": "shots_total",
        "human": "shot location",
        "means": "share of shots taken inside the box",
        "high": "works the ball into the box", "low": "shoots from distance",
        "measured_range": "0.82 (Arsenal) -> 0.57 (Forest), EPL 2025 n=40",
    },
    "chance_quality": {
        "kind": "rate",
        "numerator": "xg", "denominator": "shots_total",
        "human": "chance quality",
        "means": "expected goals per shot",
        "high": "few, good chances", "low": "many, poor chances",
        "measured_range": "0.164 (Chelsea) -> 0.068 (Forest), EPL 2025 n=40",
    },
    "defensive_line_height": {
        "kind": "level",
        "field": "offsides_drawn",
        "human": "defensive line height",
        "means": ("offsides DRAWN per match — the OPPONENT's offside count. A "
                  "high line is what forces them, so the count belongs to the "
                  "team that forced it, never to the team that committed it"),
        "high": "high defensive line", "low": "deep defensive line",
        "measured_range": "3.00 (Villa) -> 0.50 (Newcastle), EPL 2025 n=40",
    },
    "defensive_load": {
        "kind": "level",
        "field": "gk_saves",
        "human": "defensive load",
        "means": "goalkeeper saves per match",
        "high": "keeper under siege", "low": "keeper untroubled",
        "measured_range": "4.2 (Forest) -> 0.3 (Arsenal), EPL 2025 n=40",
    },
}

# The SIXTH CANDIDATE, held apart on purpose. A team's OWN offsides is a
# different quantity from the offsides it draws (a verticality proxy: how often
# it plays a runner beyond the last defender). Whether it adds separation
# BEYOND the five is measured by `separation_report` and `correlation_report`
# like any other axis — but it is not one of the five, because promoting it on
# the strength of its own measurement would be the post-hoc axis S-10's
# falsifier explicitly forbids.
CANDIDATE_AXES: dict[str, dict] = {
    "verticality": {
        "kind": "level",
        "field": "offsides_own",
        "human": "verticality",
        "means": "a team's OWN offsides per match — running beyond the line",
        "high": "plays runners beyond the line", "low": "keeps the ball ahead",
        "measured_range": "UNMEASURED — this is the candidate, not a finding",
    },
}

ALL_AXES: dict[str, dict] = {**AXES, **CANDIDATE_AXES}

# Two axes correlating at or above this in a league may be ONE axis there. Not
# a law — a threshold declared before the fit so the verdict is not chosen
# after seeing the matrix.
COLLINEAR_ABS_R = 0.8

# WHICH SOURCE CAN CARRY WHICH AXIS. Declared as data and TESTED, because it
# is the difference between "this league's axis did not separate" and "this
# league never had that axis". Sportec (MLS) publishes shots inside/outside
# box and xG but NO possession, NO offsides and NO goalkeeper saves, so an MLS
# style vector is structurally a 2-axis vector, not a 5-axis one with three
# zeros. Reporting it as five would be missing evidence converted into data.
SOURCE_AXES: dict[str, tuple[str, ...]] = {
    SOURCE_APIFOOTBALL: ("ball_dominance", "shot_location", "chance_quality",
                         "defensive_line_height", "defensive_load",
                         "verticality"),
    SOURCE_SPORTEC: ("shot_location", "chance_quality"),
}

# Refusal reasons an axis can carry instead of a coordinate. Each is a
# DIFFERENT truth and gets different words on any surface — never a blank,
# never a zero.
AXIS_NOT_IN_SOURCE = "axis_not_published_by_source"
AXIS_NO_OBSERVATIONS = "axis_has_no_usable_observations"
AXIS_BELOW_SAMPLE_FLOOR = "axis_below_fixture_floor"
AXIS_DOES_NOT_SEPARATE = "axis_does_not_separate_in_this_league"
NOT_INGESTED = "league_style_not_ingested"

REFUSAL_WORDS = {
    AXIS_NOT_IN_SOURCE: ("this provider does not publish the statistic this "
                         "axis is built from"),
    AXIS_NO_OBSERVATIONS: "no usable observations for this axis",
    AXIS_BELOW_SAMPLE_FLOOR: "not enough fixtures to place this team on the axis",
    AXIS_DOES_NOT_SEPARATE: ("every team in this league-season scored the same "
                             "on this axis — there is nothing to standardise"),
    NOT_INGESTED: "no style observations held for this league yet",
}

STYLE_ATTRIBUTION = (
    "Playstyle coordinates: each axis is standardised WITHIN this league-"
    "season against that league's own distribution, so a value describes a "
    "team relative to its own league and nothing else. Two teams' vectors "
    "from different leagues are NOT comparable, and no match probability is "
    "or can be derived from these numbers.")


class LabelIsNotAnInput(TypeError):
    """Raised when code tries to compute with a rendered style label.

    The refusal IS the feature. Coordinates are the representation; the label
    is a rendering of them for a human. A model that consumed the label would
    be fitting on a lossy bucketing of numbers it already has, and would
    silently inherit whatever arbitrary cutoffs the renderer used."""


@dataclass(frozen=True, eq=False)
class DisplayLabel:
    """A human-readable style name that CANNOT be used as an input.

    Every dunder that would let this participate in a computation raises. The
    only sanctioned exits are `str()` and `for_display()` — one greppable seam,
    named for the only thing it is for, exactly as `ScopedStrength.for_display`
    is. `parts` is kept so a surface can render the reasoning behind the name,
    and it is a tuple of plain strings because those are for a template too."""
    text: str
    parts: tuple[str, ...] = ()

    def for_display(self) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"DisplayLabel({self.text!r})"

    def _refuse(self, op: str):
        raise LabelIsNotAnInput(
            f"refusing to {op} a rendered style label ({self.text!r}). The "
            "label is a RENDERING of the style vector for a human; the vector "
            "is the representation. Compute on the coordinates "
            "(`StyleVector.axes`) — via `style_covariates` — and call "
            "`for_display()` only on the way to a template.")

    # comparison and hashing: refused, so a label can neither be branched on
    # nor used as a dict/set key (which is how a label becomes a category)
    def __eq__(self, other):
        self._refuse("compare")

    def __ne__(self, other):
        self._refuse("compare")

    def __hash__(self):
        self._refuse("hash")

    def __lt__(self, other):
        self._refuse("order")

    def __gt__(self, other):
        self._refuse("order")

    def __le__(self, other):
        self._refuse("order")

    def __ge__(self, other):
        self._refuse("order")

    # truthiness: refused, so `if label:` cannot silently gate behaviour
    def __bool__(self):
        self._refuse("test the truthiness of")

    # arithmetic and numeric coercion
    def __add__(self, other):
        self._refuse("add")

    def __radd__(self, other):
        self._refuse("add")

    def __mul__(self, other):
        self._refuse("multiply")

    def __rmul__(self, other):
        self._refuse("multiply")

    def __sub__(self, other):
        self._refuse("subtract")

    def __float__(self):
        self._refuse("coerce to float")

    def __int__(self):
        self._refuse("coerce to int")

    def __index__(self):
        self._refuse("index with")

    # containment and iteration, the other two routes to a category
    def __len__(self):
        self._refuse("take the length of")

    def __contains__(self, item):
        self._refuse("test membership in")

    def __iter__(self):
        self._refuse("iterate")


# --- parsing: absent is never zero ---------------------------------------

_ABSENT_STRINGS = {"-", "–", "—", "null", "none", "n/a", "na", ""}


def parse_percent(raw: object) -> float | None:
    """A percentage as a float in 0-100, or None for ABSENCE.

    API-Football sends possession as a STRING with a trailing '%' ('27%').
    None / '' / '-' / non-numeric are absence. Unlike `apifootball.parse_xg`,
    a literal 0 is KEPT here: 0% possession is not a value the provider uses
    as a placeholder for this statistic the way it does for xG, and it is
    physically distinguishable from missing. A value outside 0-100 is absence,
    because it is not a percentage."""
    v = _numeric(raw)
    if v is None:
        return None
    return v if 0.0 <= v <= 100.0 else None


def parse_count(raw: object) -> int | None:
    """A non-negative event count, or None for ABSENCE.

    A genuine zero IS data here — a team really can take no shots, draw no
    offsides and make no saves, and those are the informative tails of three
    of the five axes. That is the opposite of `apifootball.parse_xg`'s rule,
    and deliberately so: the provider uses null and 0 interchangeably as xG
    placeholders, which is what makes a zero unreadable THERE. For counts it
    sends null for absent and an integer for present, so treating 0 as absent
    would discard exactly the teams the axis is trying to find."""
    v = _numeric(raw)
    if v is None:
        return None
    if v < 0 or v != int(v):
        return None
    return int(v)


def _numeric(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if s.lower() in _ABSENT_STRINGS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# API-Football statistic TYPE strings, measured against the live feed on
# 2026-07-29 rather than guessed. `_norm` folds case and non-alphanumerics so
# 'Shots insidebox' and 'Shots Inside Box' both land.
_TYPE_MAP = {
    "ballpossession": ("possession_pct", parse_percent),
    "totalshots": ("shots_total", parse_count),
    "shotsinsidebox": ("shots_inside_box", parse_count),
    "expectedgoals": ("xg", None),          # reuses apifootball.parse_xg
    "offsides": ("offsides_own", parse_count),
    "goalkeepersaves": ("gk_saves", parse_count),
}


def _norm(t: object) -> str:
    return "".join(c for c in str(t or "").lower() if c.isalnum())


def read_fixture_style(payload: object) -> list[dict]:
    """Both teams' raw style inputs from one `fixtures/statistics` response.

    PURE. -> [{provider_team_id, team_name, possession_pct, shots_total,
    shots_inside_box, xg, offsides_own, gk_saves, raw}] in provider order.

    `offsides_drawn` is NOT set here, because it is not a property of one team's
    own statistics block — it is the OPPONENT's offside count, and pairing is
    what makes it available. `pair_offsides_drawn` does that, and it is a
    separate step so a caller holding only one side cannot silently end up with
    a half-derived axis."""
    out: list[dict] = []
    for entry in apifootball.response_items(payload):
        if not isinstance(entry, dict):
            continue
        team = entry.get("team") or {}
        stats = entry.get("statistics")
        stats = stats if isinstance(stats, list) else []
        rec: dict = {"provider_team_id": team.get("id"),
                     "team_name": team.get("name"), "raw": {}}
        for key in ("possession_pct", "shots_total", "shots_inside_box",
                    "xg", "offsides_own", "gk_saves"):
            rec[key] = None
        for st in stats:
            if not isinstance(st, dict):
                continue
            hit = _TYPE_MAP.get(_norm(st.get("type")))
            if hit is None:
                continue
            key, parser = hit
            if rec[key] is not None:
                continue                    # first occurrence wins, as in
            raw = st.get("value")           # apifootball.read_fixture_stats
            rec["raw"][key] = None if raw is None else str(raw)[:16]
            rec[key] = (apifootball.parse_xg(raw) if parser is None
                        else parser(raw))
        out.append(rec)
    return out


def pair_offsides_drawn(teams: list[dict]) -> list[dict]:
    """Attach each team's `offsides_drawn` — the OTHER team's own offsides.

    Requires EXACTLY two teams. A one-sided response cannot support this axis,
    and inventing a 0 for the missing side would fabricate a deep defensive
    line for whichever team the provider happened to drop."""
    if len(teams) != 2:
        return [{**t, "offsides_drawn": None} for t in teams]
    a, b = teams
    return [{**a, "offsides_drawn": b.get("offsides_own")},
            {**b, "offsides_drawn": a.get("offsides_own")}]


def style_value_hash(rec: dict) -> str:
    """The discriminator that turns a silent overwrite into a new row.

    Hashes the PROVIDER'S OWN STRINGS where they were kept, so a provider that
    changes '27%' to '27.0%' is visible too — a formatting change is still a
    change in what we were told. `offsides_drawn` is included as a parsed int
    because it is derived from the other side's string, which is itself hashed
    in that side's row."""
    canon = json.dumps(
        {"raw": {k: rec.get("raw", {}).get(k)
                 for k in sorted(("possession_pct", "shots_total",
                                  "shots_inside_box", "xg", "offsides_own",
                                  "gk_saves"))},
         "offsides_drawn": rec.get("offsides_drawn")},
        separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


# --- per-fixture axis values (pure) --------------------------------------

def axis_terms(row: dict, axis: str) -> tuple[float, float] | None:
    """(numerator, denominator) contributed by ONE team-fixture to one axis,
    or None when this fixture cannot inform this axis.

    A level axis contributes (value, 1.0); a rate axis contributes
    (numerator, denominator) so the aggregate is a ratio of weighted sums
    rather than a mean of ratios. Returning None — not (0, 0) — is what keeps
    an absent statistic out of both `n_obs` and the mean."""
    spec = ALL_AXES.get(axis)
    if spec is None:
        raise KeyError(f"{axis!r} is not a registered style axis")
    if spec["kind"] == "level":
        v = row.get(spec["field"])
        return None if v is None else (float(v), 1.0)
    num = row.get(spec["numerator"])
    den = row.get(spec["denominator"])
    if num is None or den is None:
        return None
    den = float(den)
    if den <= 0:
        # zero shots is a real match, but 'inside-box share of no shots' and
        # 'xG per no shots' are undefined, not zero. The fixture informs the
        # denominator of nothing.
        return None
    return (float(num), den)


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _weight(days_ago: float, half_life: float) -> float:
    return 0.5 ** (max(days_ago, 0.0) / half_life)


# --- the fitted vector ---------------------------------------------------

@dataclass(frozen=True)
class AxisCoordinate:
    """One team's position on one axis inside one league-season.

    `z` is the standardised coordinate — the thing to compute with — and it is
    a `ScopedStrength`, so differencing it against another league's is refused
    by the type rather than by a comment. `raw` and the league moments ride
    along so a future reader can RE-STANDARDISE without refitting, which is the
    whole reason raw is stored beside z."""
    scope: XgScope
    axis: str
    z: ScopedStrength
    raw: float                  # this team's recency-weighted own value
    shrunk: float               # after partial pooling toward the league mean
    league_mean: float
    league_sd: float
    n_obs: int                  # fixtures that actually carried this axis
    weight_sum: float

    def as_dict(self) -> dict:
        return {"axis": self.axis, "z": round(self.z.for_display(), 4),
                "raw": round(self.raw, 4), "shrunk": round(self.shrunk, 4),
                "league_mean": round(self.league_mean, 4),
                "league_sd": round(self.league_sd, 4),
                "n_obs": self.n_obs,
                "competition_key": self.scope.key,
                "comparable_across_competitions": False}


@dataclass(frozen=True)
class StyleVector:
    """One team's playstyle coordinates inside ONE league-season.

    THERE IS NO LABEL FIELD, AND THAT IS THE DESIGN. A label is a rendering of
    these coordinates for a human; `render_label(vector)` produces one on
    demand and returns a `DisplayLabel` that refuses to be computed with. If a
    label were stored here it would be one attribute access away from becoming
    a model input, and the next author would take it.

    `refused` carries a NAMED reason for every axis this team has no
    coordinate on — never a blank, never a 0.0. An axis the provider does not
    publish and an axis the team has too few fixtures for are different facts
    and read differently."""
    scope: XgScope
    provider_team_id: int
    team_name: str
    axes: dict[str, AxisCoordinate]
    refused: dict[str, str] = field(default_factory=dict)
    n_fixtures: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    shrink_games: float = 0.0
    half_life_days: float = 0.0

    @property
    def source(self) -> str:
        return self.scope.source

    @property
    def competition_key(self) -> str:
        return self.scope.key

    def z(self, axis: str) -> ScopedStrength | None:
        """The scoped coordinate on one axis, or None when refused. Returning
        None rather than 0.0 forces a caller to handle absence."""
        c = self.axes.get(axis)
        return None if c is None else c.z

    def as_dict(self) -> dict:
        """Serialization for an API. Coordinates are unwrapped here — once, at
        the boundary — and `comparable_across_competitions` ships as an
        explicit False rather than being left to a reader's judgement.

        The label is rendered through `for_display()`, the single sanctioned
        exit, so a grep for that call finds every place a label leaves the
        display path."""
        return {
            "provider_team_id": self.provider_team_id,
            "team_name": self.team_name,
            "competition_key": self.competition_key,
            "source": self.source,
            "league_id": self.scope.league_id, "season": self.scope.season,
            "axes": {k: v.as_dict() for k, v in self.axes.items()},
            "refused": {k: {"reason": v,
                            "reason_words": REFUSAL_WORDS.get(v, v)}
                        for k, v in self.refused.items()},
            "n_fixtures": self.n_fixtures,
            "window_start": (self.window_start.isoformat()
                             if self.window_start else None),
            "window_end": (self.window_end.isoformat()
                           if self.window_end else None),
            "shrink_games": self.shrink_games,
            "half_life_days": self.half_life_days,
            "display_label": render_label(self).for_display(),
            "label_is_display_only": True,
            "comparable_across_competitions": False,
            "attribution": STYLE_ATTRIBUTION,
        }


def pair_for_style_model(home: StyleVector, away: StyleVector):
    """The one gate any fixture-level use of two style vectors must pass.

    Refuses a cross-competition pair outright, mirroring
    `xg_ratings.pair_for_pricing` and raising the SAME exception type — one
    refusal vocabulary, so a caller catching cross-competition errors catches
    both. Reachable by name from anywhere that might build a style-interaction
    model, including a caller that never touches an `AxisCoordinate`
    directly."""
    if home.scope != away.scope:
        raise CrossCompetitionArithmetic(
            f"refusing to pair style vectors from different competitions "
            f"({home.scope.key} vs {away.scope.key}). Every axis is "
            "standardised against its OWN league-season's distribution, so a "
            "difference between two of them has no referent — 55% possession "
            "is a different fact in the Championship than in La Liga. Display "
            "both; model from neither.")
    return home, away


# --- the fit -------------------------------------------------------------

def fit_style_vectors(rows: list[dict], scope: XgScope,
                      as_of: datetime | None = None,
                      shrink_games: float | None = None,
                      half_life: float | None = None,
                      min_fixtures: int | None = None,
                      axes: dict | None = None) -> dict:
    """Per-team style vectors for ONE league-season.

    Pure function of its inputs — no database, no network, no clock unless
    `as_of` is omitted — so it is directly testable and a walk-forward caller
    can hand it a prior-only slice.

    Construction mirrors `xg_ratings.fit_league_ratings`, which mirrors
    `model_mls.fit`'s xG arm: each team's axis value is recency-weighted with
    the MLS half-life, then shrunk toward the league's own mean by a prior
    weight expressed in games. A fixture whose statistic is absent contributes
    NOTHING — it is not counted as a zero and does not inflate `n_obs`.

    STANDARDISATION IS WITHIN THIS LEAGUE-SEASON AND NOWHERE ELSE. `league_sd`
    is the population sd of the SHRUNK team values in this scope only. Where
    that sd is 0 the axis is REFUSED (`AXIS_DOES_NOT_SEPARATE`) rather than
    divided by, so a league where an axis carries no information produces named
    refusals instead of a column of infinities.

    -> {"scope", "vectors": {team_id: StyleVector}, "league": {axis: moments},
        "n_team_matches", "axes_available", "axes_not_in_source"}
    """
    as_of = _utc(as_of) or datetime.now(timezone.utc)
    k = (shrink_games if shrink_games is not None
         else config.TEAM_STYLE_SHRINK_GAMES)
    hl = half_life if half_life is not None else HALF_LIFE_DAYS
    need = (min_fixtures if min_fixtures is not None
            else config.TEAM_STYLE_MIN_FIXTURES)
    registry = axes if axes is not None else ALL_AXES

    publishes = set(SOURCE_AXES.get(scope.source, ()))
    active = [a for a in registry if a in publishes]
    absent_axes = [a for a in registry if a not in publishes]

    usable = [r for r in rows if r.get("provider_team_id") is not None
              and r.get("kickoff_utc") is not None]
    # per (team, axis): weighted numerator / denominator / weight / count
    num: dict[tuple[int, str], float] = {}
    den: dict[tuple[int, str], float] = {}
    wgt: dict[tuple[int, str], float] = {}
    cnt: dict[tuple[int, str], int] = {}
    lnum: dict[str, float] = {a: 0.0 for a in active}
    lden: dict[str, float] = {a: 0.0 for a in active}
    names: dict[int, str] = {}
    first: dict[int, datetime] = {}
    last: dict[int, datetime] = {}
    fixtures_seen: dict[int, set] = {}

    for r in usable:
        tid = r["provider_team_id"]
        ko = _utc(r["kickoff_utc"])
        w = _weight((as_of - ko).total_seconds() / 86400, hl)
        if r.get("team_name"):
            names[tid] = r["team_name"]
        if tid not in first or ko < first[tid]:
            first[tid] = ko
        if tid not in last or ko > last[tid]:
            last[tid] = ko
        fixtures_seen.setdefault(tid, set()).add(r.get("provider_fixture_id"))
        for a in active:
            terms = axis_terms(r, a)
            if terms is None:
                continue
            n_, d_ = terms
            key = (tid, a)
            num[key] = num.get(key, 0.0) + w * n_
            den[key] = den.get(key, 0.0) + w * d_
            wgt[key] = wgt.get(key, 0.0) + w
            cnt[key] = cnt.get(key, 0) + 1
            lnum[a] += w * n_
            lden[a] += w * d_

    if not usable:
        return {"scope": scope, "vectors": {}, "league": {},
                "n_team_matches": 0, "axes_available": [],
                "axes_not_in_source": absent_axes, "reason": NOT_INGESTED}

    # league mean per axis: the same ratio-of-weighted-sums the teams use, so
    # the centre and the values it centres are the same construction
    league_mean = {a: (lnum[a] / lden[a]) for a in active if lden[a] > 0}
    thin_axes = [a for a in active if a not in league_mean]

    # stage 1: each team's own value and its shrunk value
    raw_v: dict[tuple[int, str], float] = {}
    shr_v: dict[tuple[int, str], float] = {}
    below: dict[tuple[int, str], str] = {}
    for (tid, a), d_ in den.items():
        if a not in league_mean or d_ <= 0:
            continue
        if cnt[(tid, a)] < need:
            below[(tid, a)] = AXIS_BELOW_SAMPLE_FLOOR
            continue
        own = num[(tid, a)] / d_
        w = wgt[(tid, a)]
        raw_v[(tid, a)] = own
        shr_v[(tid, a)] = (w * own + k * league_mean[a]) / (w + k)

    # stage 2: standardise WITHIN this league-season, over the shrunk values
    league_sd: dict[str, float] = {}
    for a in league_mean:
        vals = [v for (tid, ax), v in shr_v.items() if ax == a]
        league_sd[a] = (statistics.pstdev(vals) if len(vals) > 1 else 0.0)

    vectors: dict[int, StyleVector] = {}
    for tid in sorted(set(t for t, _ in den) | set(names)):
        coords: dict[str, AxisCoordinate] = {}
        refused: dict[str, str] = {}
        for a in absent_axes:
            refused[a] = AXIS_NOT_IN_SOURCE
        for a in thin_axes:
            refused[a] = AXIS_NO_OBSERVATIONS
        for a in league_mean:
            key = (tid, a)
            if key in below:
                refused[a] = below[key]
                continue
            if key not in shr_v:
                refused[a] = AXIS_NO_OBSERVATIONS
                continue
            sd = league_sd[a]
            if sd <= 0:
                refused[a] = AXIS_DOES_NOT_SEPARATE
                continue
            mu = statistics.fmean(
                [v for (t2, ax), v in shr_v.items() if ax == a])
            coords[a] = AxisCoordinate(
                scope=scope, axis=a,
                z=ScopedStrength(scope, (shr_v[key] - mu) / sd),
                raw=raw_v[key], shrunk=shr_v[key],
                league_mean=league_mean[a], league_sd=sd,
                n_obs=cnt[key], weight_sum=wgt[key])
        vectors[tid] = StyleVector(
            scope=scope, provider_team_id=tid,
            team_name=names.get(tid, str(tid)),
            axes=coords, refused=refused,
            n_fixtures=len(fixtures_seen.get(tid, ())),
            window_start=first.get(tid), window_end=last.get(tid),
            shrink_games=k, half_life_days=hl)

    return {
        "scope": scope, "vectors": vectors,
        "league": {a: {"mean": league_mean[a], "sd": league_sd[a]}
                   for a in league_mean},
        "n_team_matches": len(usable),
        "axes_available": sorted(league_mean),
        "axes_not_in_source": absent_axes,
        "axes_without_observations": thin_axes,
    }


# --- the label: derived, display-only -----------------------------------

# Cutoffs for turning coordinates into words. They are ARBITRARY, which is
# precisely why the label may never be an input: a model fitted on these
# buckets would be fitting on this constant.
_STRONG_Z = 0.8


def render_label(vector: StyleVector) -> DisplayLabel:
    """A human-readable style name DERIVED from the coordinates.

    Display only, and structurally so: the return type refuses every
    arithmetic, comparison, hashing, truthiness and coercion operation, so this
    string cannot become a category, a bucket or a feature. See
    `LabelIsNotAnInput`.

    The wording is built from the axes the team ACTUALLY has coordinates on. A
    team whose provider publishes two axes gets a two-axis description rather
    than a five-axis one with three silent zeros — the label inherits the same
    absent-is-never-zero rule as everything else here."""
    parts: list[str] = []
    for axis in ALL_AXES:
        c = vector.axes.get(axis)
        if c is None:
            continue
        z = c.z.for_display()
        if z >= _STRONG_Z:
            parts.append(ALL_AXES[axis]["high"])
        elif z <= -_STRONG_Z:
            parts.append(ALL_AXES[axis]["low"])
    if not parts:
        if not vector.axes:
            return DisplayLabel("no style measured", ())
        return DisplayLabel("league-typical", ())
    return DisplayLabel(", ".join(parts[:3]), tuple(parts))


# --- model inputs: coordinates only -------------------------------------

def style_covariates(home: StyleVector, away: StyleVector,
                     axes: tuple[str, ...] | None = None) -> dict:
    """The per-fixture style covariates a model may consume.

    THE ONLY sanctioned route from style vectors to model inputs, and the
    reason it is a single named function is so a reviewer has one place to
    check. Three properties, each enforced rather than documented:

      1. It refuses a cross-competition pair, through `pair_for_style_model`.
      2. It iterates ONLY keys in the frozen registry. A key not in `ALL_AXES`
         raises — so a post-hoc axis cannot arrive as a string, and neither can
         a label-derived category.
      3. It refuses a `DisplayLabel` anywhere in its arguments, by type.

    Each covariate is the HOME-MINUS-AWAY difference of the two teams'
    coordinates on one axis, computed through `ScopedStrength.__sub__` so the
    scope check runs on every single subtraction rather than only at the gate.
    An axis either side lacks is ABSENT from the output — never 0.0, which
    would assert the two teams were identical on it."""
    for name, val in (("home", home), ("away", away)):
        if isinstance(val, DisplayLabel):
            raise LabelIsNotAnInput(
                f"refusing to build covariates from a rendered label passed as "
                f"{name!r}. Pass the StyleVector; the label is a rendering.")
        if not isinstance(val, StyleVector):
            raise TypeError(f"{name} must be a StyleVector, got "
                            f"{type(val).__name__}")
    pair_for_style_model(home, away)
    wanted = tuple(ALL_AXES) if axes is None else tuple(axes)
    unknown = [a for a in wanted if a not in ALL_AXES]
    if unknown:
        raise KeyError(
            f"{unknown!r} are not registered style axes. A new axis is a new "
            "hypothesis owed its own pre-registration (BACKLOG S-10's "
            "falsifier), not a string passed at the call site.")
    out: dict[str, float] = {}
    absent: dict[str, str] = {}
    for a in wanted:
        h, v = home.z(a), away.z(a)
        if h is None or v is None:
            absent[a] = (home.refused.get(a) or away.refused.get(a)
                         or AXIS_NO_OBSERVATIONS)
            continue
        out[f"style_diff_{a}"] = (h - v).for_display()
    return {"covariates": out, "absent": absent,
            "competition_key": home.competition_key,
            "n_axes": len(out)}


# --- separation and correlation: the measurement, per league -------------

def separation_report(fit: dict) -> dict:
    """Does each axis actually SEPARATE the teams in this league-season?

    Reports, per axis: the number of teams placed, the raw spread (max - min of
    the teams' own recency-weighted values), the raw population sd, and the
    shrunk sd that standardisation divides by. Raw AND shrunk are both given
    because shrinkage compresses spread — reporting only the shrunk figure
    would understate real separation, and reporting only the raw one would
    overstate what the z-scores are actually built on.

    `separates` is False where the shrunk sd is 0: that is a league in which
    the axis carries no information, and it must be visible rather than
    assumed. An axis the source does not publish is reported separately again
    — 'not measured here' and 'measured flat' are different findings."""
    vectors = fit.get("vectors") or {}
    rows = {}
    for axis in ALL_AXES:
        placed = [(v.team_name, v.axes[axis])
                  for v in vectors.values() if axis in v.axes]
        if not placed:
            reasons = {v.refused.get(axis) for v in vectors.values()}
            reasons.discard(None)
            rows[axis] = {
                "axis": axis, "human": ALL_AXES[axis]["human"],
                "n_teams": 0, "separates": False,
                "refused_reason": (sorted(reasons)[0] if reasons
                                   else AXIS_NO_OBSERVATIONS),
                "refused_words": REFUSAL_WORDS.get(
                    sorted(reasons)[0] if reasons else AXIS_NO_OBSERVATIONS),
            }
            continue
        raws = [c.raw for _, c in placed]
        shrunks = [c.shrunk for _, c in placed]
        lo = min(placed, key=lambda p: p[1].raw)
        hi = max(placed, key=lambda p: p[1].raw)
        sd_shrunk = (statistics.pstdev(shrunks) if len(shrunks) > 1 else 0.0)
        rows[axis] = {
            "axis": axis, "human": ALL_AXES[axis]["human"],
            "candidate": axis in CANDIDATE_AXES,
            "n_teams": len(placed),
            "raw_min": round(min(raws), 4), "raw_max": round(max(raws), 4),
            "raw_spread": round(max(raws) - min(raws), 4),
            "raw_sd": round(statistics.pstdev(raws) if len(raws) > 1 else 0.0,
                            4),
            "shrunk_sd": round(sd_shrunk, 4),
            "min_team": lo[0], "max_team": hi[0],
            "separates": sd_shrunk > 0,
            "refused_reason": None,
        }
    return {"competition_key": fit["scope"].key,
            "n_team_matches": fit.get("n_team_matches", 0),
            "axes": rows}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson r, or None when it is undefined. Undefined is a real answer: a
    constant series has no correlation, and returning 0.0 would read as
    'measured independent'."""
    if len(xs) < 3:
        return None
    try:
        sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    except statistics.StatisticsError:
        return None
    if sx <= 0 or sy <= 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs)
    return cov / (sx * sy)


def correlation_report(fit: dict) -> dict:
    """Pairwise correlation between axes, ACROSS THE TEAMS of one league-season.

    Computed on each team's raw (unstandardised) value, pairwise-complete: a
    team missing either axis is dropped from that pair only, and the n behind
    every coefficient is reported, so a coefficient resting on four teams
    cannot be read as one resting on twenty.

    A pair at or above `COLLINEAR_ABS_R` is FLAGGED as possibly one axis in
    this league. That is a flag, not a merge: the 40-fixture look suggested
    independence, a full season is the real test, and it may not hold
    everywhere. Nothing in this module acts on the flag — collapsing two axes
    into one because a fit said so would be exactly the post-hoc redesign S-10
    forbids."""
    vectors = fit.get("vectors") or {}
    keys = [a for a in ALL_AXES
            if any(a in v.axes for v in vectors.values())]
    pairs = []
    flagged = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            both = [(v.axes[a].raw, v.axes[b].raw) for v in vectors.values()
                    if a in v.axes and b in v.axes]
            r = _pearson([p[0] for p in both], [p[1] for p in both])
            rec = {"a": a, "b": b, "n_teams": len(both),
                   "r": None if r is None else round(r, 4)}
            if r is not None and abs(r) >= COLLINEAR_ABS_R:
                rec["flag"] = "may_be_one_axis_in_this_league"
                flagged.append(rec)
            pairs.append(rec)
    return {"competition_key": fit["scope"].key,
            "threshold_abs_r": COLLINEAR_ABS_R,
            "axes": keys, "pairs": pairs, "flagged": flagged,
            "note": ("r is computed on teams' raw axis values, "
                     "pairwise-complete; n_teams is the denominator behind "
                     "each coefficient. A flag is a finding to report, never "
                     "a licence to merge two axes.")}


# --- the store -----------------------------------------------------------

def _refuse_mls_from_apifootball(league_id: int, source: str) -> None:
    """MLS style must not be sourced from API-Football by default.

    The xG store refuses league 253 by name because MLS xG already exists free
    from Sportec inside the engine signature. The style reasoning is adjacent
    but not identical, and the difference matters: Sportec does NOT publish
    possession, offsides or goalkeeper saves, so three of the five axes are
    genuinely unavailable for MLS rather than merely sourced elsewhere.

    That is a real gap, and the honest response is to REPORT it — see
    `SOURCE_AXES` — not to quietly resolve it by mixing providers inside one
    competition. Mixing would put MLS's style on API-Football's measurement
    system while its xG ratings stay on Sportec's, and the two disagree by 0.35
    xG per team-observation on average (`strategy/XG-SOURCE-COMPARISON.md`).
    Whether to accept that split is Son's decision, not a default."""
    if int(league_id) == apifootball.MLS_LEAGUE_ID \
            and source == SOURCE_APIFOOTBALL:
        raise apifootball.ApiFootballError(
            "refusing to source MLS (league 253) style from API-Football. MLS "
            "is served by Sportec; three of the five axes (possession, "
            "offsides, goalkeeper saves) are NOT published by Sportec and that "
            "gap is reported by SOURCE_AXES rather than closed by mixing two "
            "measurement systems inside one competition. Reopening this is a "
            "decision, not a default.")


def _insert_style(s, source, league_id, season, f, rec, read_at) -> str:
    """Insert one (fixture, side) reading unless an identical one exists.

    -> 'written' | 'unchanged' | 'corrected'. 'corrected' means the same
    (source, fixture, side) already holds a DIFFERENT value, so the provider
    changed a historical statistic under us and BOTH readings are now on the
    record."""
    from src.live.models import TeamStyleObservation
    vh = style_value_hash(rec)
    fid = str(f["provider_fixture_id"])
    existing = (s.query(TeamStyleObservation)
                .filter_by(source=source, provider_fixture_id=fid,
                           side=rec["side"]).all())
    if any(e.value_hash == vh for e in existing):
        return "unchanged"
    kickoff = None
    if f.get("kickoff_iso"):
        try:
            kickoff = datetime.fromisoformat(
                str(f["kickoff_iso"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            kickoff = None
    s.add(TeamStyleObservation(
        source=source, league_id=league_id, season=season,
        provider_fixture_id=fid, side=rec["side"],
        provider_team_id=rec["provider_team_id"],
        team_name=(rec.get("team_name") or "")[:96] or None,
        opponent_team_id=rec.get("opponent_team_id"),
        kickoff_utc=kickoff,
        round_label=(f.get("round_label") or "")[:64] or None,
        possession_pct=rec.get("possession_pct"),
        shots_total=rec.get("shots_total"),
        shots_inside_box=rec.get("shots_inside_box"),
        xg=rec.get("xg"),
        offsides_drawn=rec.get("offsides_drawn"),
        offsides_own=rec.get("offsides_own"),
        gk_saves=rec.get("gk_saves"),
        raw_json=json.dumps(rec.get("raw") or {}, separators=(",", ":"))[:2000],
        value_hash=vh, read_at=read_at))
    return "corrected" if existing else "written"


def ingest_league_style(league_id: int, season: int,
                        client=None, max_fixtures: int | None = None,
                        skip_existing: bool = True,
                        source: str = SOURCE_APIFOOTBALL) -> dict:
    """Ingest per-fixture raw style inputs for one (league, season).

    `skip_existing` avoids the network for a fixture already held on both
    sides, which is what makes a resumed or repeated ingest cheap against a
    shared daily quota. A deliberate re-read looking for retroactive
    corrections must pass skip_existing=False — the cheap path cannot detect a
    change it never fetches.

    NOTE ON COST, recorded because it was avoidable. This spends one request
    per fixture on `fixtures/statistics` — the SAME endpoint, for the same
    fixtures, that the xG ingest already paid for. That earlier ingest parsed
    one of the eighteen statistic types the response carries and discarded the
    rest, so the style axes had to be bought twice. Anything reading this
    endpoint in future should persist every type it receives."""
    _refuse_mls_from_apifootball(league_id, source)
    if not plane_ready():
        return {"skipped": "dormant"}
    from src.live.models import TeamStyleObservation
    c = client or apifootball.Client()
    fb = c.get("fixtures", {"league": league_id, "season": season})
    fixtures = apifootball.completed_fixtures(fb)
    if max_fixtures:
        fixtures = fixtures[-max_fixtures:]
    read_at = datetime.now(timezone.utc)
    counts = {"written": 0, "unchanged": 0, "corrected": 0,
              "skipped_existing": 0, "no_stats": 0, "one_sided": 0,
              "fetch_failed": 0}
    s = get_session()
    try:
        for f in fixtures:
            fid = str(f["provider_fixture_id"])
            if skip_existing:
                have = (s.query(TeamStyleObservation)
                        .filter_by(source=source,
                                   provider_fixture_id=fid).count())
                if have >= 2:
                    counts["skipped_existing"] += 1
                    continue
            try:
                sb = c.get("fixtures/statistics",
                           {"fixture": f["provider_fixture_id"]})
            except apifootball.BudgetExceeded:
                s.commit()
                counts["aborted"] = "request budget reached"
                break
            except apifootball.ApiFootballError:
                counts["fetch_failed"] += 1
                continue
            teams = read_fixture_style(sb)
            if len(teams) != 2:
                counts["no_stats"] += 1
                continue
            teams = pair_offsides_drawn(teams)
            by_id = {t["provider_team_id"]: t for t in teams}
            home = by_id.get(f["home_team_id"])
            away = by_id.get(f["away_team_id"])
            if home is None or away is None:
                # the provider's own team ids did not line up with its own
                # fixture — never guessed by position
                counts["one_sided"] += 1
                continue
            try:
                for side, own, opp in (("home", home, away),
                                       ("away", away, home)):
                    rec = {**own, "side": side,
                           "opponent_team_id": opp["provider_team_id"]}
                    counts[_insert_style(s, source, league_id, season, f,
                                         rec, read_at)] += 1
                s.commit()
            except Exception as exc:              # isolate one bad fixture
                s.rollback()
                counts["fetch_failed"] += 1
                counts.setdefault("errors", []).append(
                    {"fixture": fid, "error": str(exc)[:160]})
        return {"source": source, "league_id": league_id, "season": season,
                "completed_fixtures": len(fixtures),
                "requests_used": c.used, "rate_limit": c.rate_limit,
                "counts": counts, "read_at": read_at.isoformat()}
    finally:
        s.close()


def style_rows(source: str, league_id: int,
               season: int | None = None) -> list[dict]:
    """The LATEST reading per (fixture, side) for one league, for the fit.

    'Latest' matters because the store is append-only: where a provider
    correction produced two readings, the fit uses the most recent one and
    `retroactive_style_corrections` is what surfaces that it happened. Silently
    averaging two readings would blend a superseded value into a current
    vector."""
    if not plane_ready():
        return []
    from src.live.models import TeamStyleObservation
    s = get_session()
    try:
        q = s.query(TeamStyleObservation).filter_by(source=source,
                                                    league_id=league_id)
        if season is not None:
            q = q.filter_by(season=season)
        latest: dict[tuple, object] = {}
        for r in q.all():
            key = (r.provider_fixture_id, r.side)
            cur = latest.get(key)
            if cur is None or (r.read_at and cur.read_at
                               and r.read_at > cur.read_at) \
                    or (cur.read_at is None and r.read_at is not None):
                latest[key] = r
        return [{
            "provider_fixture_id": r.provider_fixture_id, "side": r.side,
            "source": r.source, "league_id": r.league_id, "season": r.season,
            "provider_team_id": r.provider_team_id, "team_name": r.team_name,
            "opponent_team_id": r.opponent_team_id,
            "kickoff_utc": r.kickoff_utc, "round_label": r.round_label,
            "possession_pct": r.possession_pct,
            "shots_total": r.shots_total,
            "shots_inside_box": r.shots_inside_box,
            "xg": r.xg,
            "offsides_drawn": r.offsides_drawn,
            "offsides_own": r.offsides_own,
            "gk_saves": r.gk_saves,
            "read_at": r.read_at,
        } for r in latest.values()]
    finally:
        s.close()


def retroactive_style_corrections(source: str | None = None) -> dict:
    """(source, fixture, side) triples holding MORE THAN ONE distinct reading.

    Zero is the expected answer and a non-zero one is a finding, not an error:
    it means the provider changed a historical statistic under us. The
    append-only store is what makes this question answerable at all."""
    if not plane_ready():
        return {"dormant": True}
    from sqlalchemy import func

    from src.live.models import TeamStyleObservation
    s = get_session()
    try:
        q = (s.query(TeamStyleObservation.source,
                     TeamStyleObservation.provider_fixture_id,
                     TeamStyleObservation.side,
                     func.count(TeamStyleObservation.id).label("n"))
             .group_by(TeamStyleObservation.source,
                       TeamStyleObservation.provider_fixture_id,
                       TeamStyleObservation.side)
             .having(func.count(TeamStyleObservation.id) > 1))
        if source is not None:
            q = q.filter(TeamStyleObservation.source == source)
        rows = [{"source": r[0], "provider_fixture_id": r[1], "side": r[2],
                 "readings": r[3]} for r in q.all()]
        return {"triples_with_multiple_readings": len(rows),
                "detail": rows[:50]}
    finally:
        s.close()


def style_census() -> dict:
    """How much style evidence this store holds, per (source, league, season),
    with a PER-AXIS usable count. A single row count would let a league whose
    possession is entirely null read as covered."""
    if not plane_ready():
        return {"dormant": True}
    from sqlalchemy import distinct, func

    from src.live.models import TeamStyleObservation as T
    s = get_session()
    try:
        out = []
        for src, lid, season in (s.query(T.source, T.league_id, T.season)
                                 .distinct().all()):
            base = s.query(T).filter_by(source=src, league_id=lid,
                                        season=season)
            rec = {
                "source": src, "league_id": lid, "season": season,
                "rows": base.count(),
                "fixtures": (s.query(func.count(distinct(
                    T.provider_fixture_id)))
                    .filter_by(source=src, league_id=lid, season=season)
                    .scalar() or 0),
                "teams": (s.query(func.count(distinct(T.provider_team_id)))
                          .filter_by(source=src, league_id=lid, season=season)
                          .scalar() or 0),
                "usable_rows_per_axis": {},
            }
            for axis, spec in ALL_AXES.items():
                cols = ([spec["field"]] if spec["kind"] == "level"
                        else [spec["numerator"], spec["denominator"]])
                q = base
                for col in cols:
                    q = q.filter(getattr(T, col).isnot(None))
                rec["usable_rows_per_axis"][axis] = q.count()
            out.append(rec)
        out.sort(key=lambda r: (r["source"], r["league_id"], r["season"]))
        return {"leagues": out}
    finally:
        s.close()


def league_style(source: str, league_id: int, season: int | None = None,
                 as_of: datetime | None = None) -> dict:
    """Fitted style vectors for one league-season, read from the store, with
    the separation and correlation measurements beside them.

    The two reports ship WITH the vectors rather than behind a separate call,
    so a consumer cannot read a coordinate without being able to see whether
    the axis it sits on separates anything in that league."""
    rows = style_rows(source, league_id, season)
    if not rows:
        return {"source": source, "league_id": league_id, "season": season,
                "vectors": {}, "refused_reason": NOT_INGESTED,
                "refused_words": REFUSAL_WORDS[NOT_INGESTED]}
    use_season = season if season is not None else rows[0]["season"]
    scope = XgScope(source=source, league_id=league_id, season=use_season)
    fit = fit_style_vectors(rows, scope, as_of=as_of)
    return {"source": source, "league_id": league_id, "season": use_season,
            "scope": scope.key, "vectors": fit["vectors"],
            "league": fit["league"],
            "axes_available": fit["axes_available"],
            "axes_not_in_source": fit["axes_not_in_source"],
            "n_team_matches": fit["n_team_matches"],
            "separation": separation_report(fit),
            "correlation": correlation_report(fit),
            "refused_reason": None,
            "attribution": STYLE_ATTRIBUTION}


def summary() -> dict:
    """What this surface can and cannot say right now."""
    return {
        "axes": {k: {kk: vv for kk, vv in v.items()} for k, v in AXES.items()},
        "candidate_axes": CANDIDATE_AXES,
        "source_axes": {k: list(v) for k, v in SOURCE_AXES.items()},
        "census": style_census(),
        "retroactive_corrections": retroactive_style_corrections(),
        "parameters": {
            "shrink_games": config.TEAM_STYLE_SHRINK_GAMES,
            "half_life_days": HALF_LIFE_DAYS,
            "min_fixtures": config.TEAM_STYLE_MIN_FIXTURES,
            "collinear_abs_r": COLLINEAR_ABS_R,
            "provenance": (
                "shrink and half-life are STARTING POINTS carried from the "
                "league xG ratings (which carried them from MLS league play), "
                "NOT swept on style data. Nothing prices off these vectors — "
                "the rung that would test them is BACKLOG S-10, unfitted."),
        },
        "attribution": STYLE_ATTRIBUTION,
        "label_policy": (
            "Coordinates are the representation; the label is a rendering. "
            "render_label() returns a DisplayLabel whose arithmetic, "
            "comparison, hashing, truthiness and coercion all raise, so a "
            "label cannot become a model input. style_covariates() is the only "
            "route from vectors to model inputs and refuses both labels and "
            "unregistered axis keys."),
    }
