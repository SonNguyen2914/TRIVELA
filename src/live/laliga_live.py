"""la-liga-2026 live-plane wiring — competition-keyed reuse, not a fork.

Everything here is a thin, league-named binding of the SHARED
competition-parameterized machinery (identity / ingest / markets /
runs) to La Liga's slug, ESPN path, Kalshi series config and model
scaffold. No table is duplicated — the live schema was already keyed
by competition — and no MLS behavior changes: every shared function
defaults to its previous MLS values.

The binding shape is EPL's (see src/live/epl_plane.py): the shared
modules take one `CompetitionMarketSpec` / `CompetitionRunsSpec`
object rather than a scatter of per-call keyword arguments. La Liga was
built against the earlier keyword form and was rebased onto this one;
the parameterization is a different spelling of the same thing, and
every La Liga behavior below — slug, ESPN esp.1, the KXLALIGA* config,
the 17 evidence-backed Kalshi bridges, the dark model, the flag
defaulting false — is unchanged by the move.

Operating posture (2026-07-28):
  - LALIGA_SHADOW_ENABLED defaults FALSE. Even when flipped on, the
    model is DARK (no approval decision for laliga-2026-v0), so runs
    and T-10 locks are refused at the per-model gates; only identity,
    fixture ingest, market discovery and quote capture would operate.
  - Kalshi series existence verified 2026-07-28, coverage NOT — zero
    open events. Discovery reports the honest unmapped state, and
    shadow_status() carries the unmapped_upcoming metric.
  - The three promoted clubs have no verifiable Kalshi name yet; they
    are absent from KALSHI_BRIDGES on purpose and will surface in the
    unmapped metric until the venue lists them and an operator curates
    the aliases.
"""
from __future__ import annotations

import config
from src.live import identity, ingest, markets, runs
from src.live import model_laliga
from src.live.db import get_session, plane_ready

SLUG = "la-liga-2026"
LEAGUE_PATH = "esp.1"
LEAGUE_LABEL = "LALIGA"
EXPECTED_TEAMS = 20

ESPN_TEAMS_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                  f"{LEAGUE_PATH}/teams")

# Operator-curated Kalshi-name bridges. Every key below was VERIFIED
# against the full 383-event historical KXLALIGAGAME slate (2025-26,
# archived 2026-07-28); values are the ESPN 2026-27 displayNames.
# Relegated sides (Oviedo, Mallorca, Girona) are deliberately absent —
# they have no 2026-27 team row to attach to. The promoted clubs
# (Deportivo La Coruña, Málaga, Racing Santander) are deliberately
# absent too: they appear in NO historical Kalshi event, and guessing
# a name is the KXMENWORLDCUP class of error. They stay unmapped and
# reported until curated from a real listing.
KALSHI_BRIDGES = {
    "Alaves": "Alavés",
    "Atletico": "Atlético Madrid",
    "Barcelona": "Barcelona",
    "Bilbao": "Athletic Club",
    "Celta Vigo": "Celta Vigo",
    "Elche": "Elche",
    "Espanyol": "Espanyol",
    "Getafe": "Getafe",
    "Levante": "Levante",
    "Osasuna": "Osasuna",
    "Real Betis": "Real Betis",
    "Real Madrid": "Real Madrid",
    "Real Sociedad": "Real Sociedad",
    "Sevilla": "Sevilla",
    "Valencia": "Valencia",
    "Vallecano": "Rayo Vallecano",
    "Villarreal": "Villarreal",
}


def ensure_competition() -> dict:
    """Idempotent la-liga-2026 Competition row. Data, not schema — the
    live tables were competition-keyed from the start, so no migration
    exists on this branch."""
    if not plane_ready():
        return {"skipped": "dormant"}
    from src.live.models import Competition
    s = get_session()
    try:
        if s.get(Competition, SLUG) is None:
            s.add(Competition(
                slug=SLUG, name="La Liga",
                # API-Football league id deliberately NOT set: the
                # free-plan key was unavailable to verify it and no
                # runtime consumer exists — verify before first use.
                provider_league_id=None,
                season=2026, timezone="UTC",
                match_duration_minutes=90, supports_draw=True,
                regular_time_only=True, has_group_stage=False,
                has_knockout_stage=False,
                model_version=model_laliga.MODEL_NAME))
            s.commit()
            return {"seeded": SLUG}
        return {"exists": SLUG}
    except Exception as exc:
        s.rollback()
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def _market_spec() -> markets.CompetitionMarketSpec:
    """La Liga's Kalshi grammar as the shared machinery's spec object.

    The series all derive from `config.LALIGA_KALSHI_SERIES_PREFIX` —
    the prefix is verified to exist, 2026-27 coverage is not, so nothing
    here is hardcoded as fact. GAME goes first because it anchors the
    fixture mapping through approved aliases and every other family
    suffix-joins to it in the same sweep."""
    from src import laliga as laliga_data
    game = laliga_data.KALSHI_GAME
    prefix = config.LALIGA_KALSHI_SERIES_PREFIX
    return markets.CompetitionMarketSpec(
        slug=SLUG,
        game_series=game,
        family_series=tuple(dict.fromkeys(
            [game] + [series for _k, series, _l
                      in laliga_data.MATCH_FAMILIES if series != game])),
        ticker_prefix=prefix,
        first_half_prefix=f"{prefix}1H",
        model_key_fn=laliga_data.model_key_for,
        # the same predicate as mls-lock-v2 (capture-clock freshness,
        # game 3-way required) under its own name, so a La Liga snapshot
        # can never claim an MLS policy string. No La Liga snapshot can
        # carry it yet: the model is dark, so no lock is reachable.
        lock_policy_version="laliga-lock-v1")


MARKET_SPEC = _market_spec()

RUNS_SPEC = runs.CompetitionRunsSpec(
    slug=SLUG, model_module=model_laliga,
    market_spec=MARKET_SPEC, label=LEAGUE_LABEL,
    expected_teams=EXPECTED_TEAMS,
    # a lambda, not the value: the flag is read at CALL time, so an env
    # flip (or a test's monkeypatch) needs no re-import
    enabled_fn=lambda: config.LALIGA_SHADOW_ENABLED)


# --- thin entry points (scheduler + API) -----------------------------------

def seed_teams() -> dict:
    return identity.seed_league_teams(SLUG, ESPN_TEAMS_URL, KALSHI_BRIDGES)


def ingest_season() -> dict:
    """Deliberately NOT season-pinned, unlike EPL: no LALIGA_SEASON_YEAR
    exists and adding one would change what this ingests."""
    return ingest.ingest_season_schedules(competition_slug=SLUG,
                                          espn_league=LEAGUE_PATH)


def refresh_window() -> dict:
    return ingest.refresh_window(competition_slug=SLUG,
                                 espn_league=LEAGUE_PATH)


def discover_and_map() -> dict:
    return markets.discover_and_map(spec=MARKET_SPEC)


def capture_quotes(fixture_id: int | None = None,
                   horizon_hours: float = 48.0) -> dict:
    return markets.capture_quotes(fixture_id=fixture_id,
                                  horizon_hours=horizon_hours,
                                  spec=MARKET_SPEC)


def scheduled_runs(freshness_hours: float = 4.0) -> dict:
    """Refused at the per-model approval gate until a La Liga ladder
    earns a decision — the dark-model posture, enforced not asserted."""
    return runs.scheduled_runs(freshness_hours=freshness_hours,
                               spec=RUNS_SPEC)


def t10_locks() -> dict:
    return runs.t10_locks(spec=RUNS_SPEC)


def model_for_event(espn_event_id: str) -> dict | None:
    return runs.model_for_event(espn_event_id, spec=RUNS_SPEC)


def latest_odds() -> list[dict]:
    return runs.latest_odds(spec=RUNS_SPEC)


def shadow_status() -> dict:
    """The public posture read: pipeline counts + blockers (including
    the unmapped_upcoming metric) + the Kalshi coverage probe + the
    explicit statements a reader needs to not over-trust this surface.

    `model_dark` and its note are DERIVED from the same two approval
    facts `counts` already reports, not asserted. They were hardcoded
    to the dark state — right only for as long as La Liga stayed
    unapproved, and the identical defect that had /api/ligamx/status
    reporting a dark model next to Liga MX's live approval decision on
    2026-07-30 (and, before that, the stale `note` fixed in 724ef54).
    Derived, the day a La Liga ladder earns an approval this surface
    moves with it instead of contradicting the counts printed directly
    beneath it. Dark stays the fail-closed answer whenever the approval
    facts cannot be read at all."""
    counts = runs.shadow_counts(spec=RUNS_SPEC) or {}
    from src import laliga as laliga_data
    approved = bool(counts.get("model_approved_for_shadow")
                    and counts.get("approval_decision_present"))
    if not counts:
        note = ("the live plane is dormant, so no approval state for "
                f"{model_laliga.MODEL_NAME} can be read here; runs and "
                "T-10 locks cannot run at all")
    elif approved:
        note = (f"{model_laliga.MODEL_NAME} has an approved "
                "model-approval decision — shadow runs and T-10 locks "
                "are permitted. This does NOT mean real money signals "
                "are enabled (they stay server-side gated) or that an "
                "edge is established")
    else:
        note = ("no approval decision exists for "
                f"{model_laliga.MODEL_NAME}; runs and T-10 "
                "locks are structurally refused until a "
                "La Liga evaluation ladder earns one")
    return {
        "competition": SLUG,
        "enabled": config.LALIGA_SHADOW_ENABLED,
        "model_version": model_laliga.MODEL_NAME,
        "model_dark": not approved,
        "model_dark_note": note,
        "xg_source": None,
        "xg_note": ("no trustworthy public La Liga xG source exists "
                    "(researched 2026-07-28) — ratings are goals-only"),
        "kalshi": laliga_data.series_probe(),
        "counts": counts,
    }


def boot() -> None:
    """One-shot La Liga plane boot: competition -> identity -> season
    history -> market map. Each step isolated; ALL of it no-ops when
    LALIGA_SHADOW_ENABLED is off (the default) or the plane is dormant.
    No approval step exists here on purpose — the model is dark, and
    only an operator-run La Liga ladder can ever change that."""
    if not config.LALIGA_SHADOW_ENABLED:
        return
    for name, step in (
            ("competition", ensure_competition),
            ("model_version_row", model_laliga.ensure_model_version),
            ("seed_teams", seed_teams),
            ("season_ingest", ingest_season),
            ("market_map", discover_and_map),
    ):
        try:
            print(f"[laliga-boot] {name}: {step()}")
        except Exception as exc:
            print(f"[laliga-boot] {name} FAILED: {exc}")
