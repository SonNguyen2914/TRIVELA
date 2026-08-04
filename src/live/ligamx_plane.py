"""Liga MX live plane — liga-mx-2026 entry points over the competition-
keyed machinery (2026-07-29).

Everything here is a thin, league-named binding of the SHARED modules
(identity / ingest / markets / runs), exactly as src/live/epl_plane.py:
the same discovery, quote-capture and T-10 lock code serves every
competition, keyed by slug. Nothing is duplicated.

State of the world this module is honest about:
  - Kalshi's KXLIGAMXGAME series is LIVE: 9 open Apertura events and
    221 historical ones on 2026-07-29, with every non-game family
    listing real markets (research_archive/ligamx_*_2026-07-29.json).
    discovery_status() still reports the live probe result instead of
    anyone asserting readiness.
  - liga-mx-2026-v0 is DARK: unapproved, no approval decision, so the
    runs machinery refuses every run and lock (F3/F9 fail-closed). The
    surfaces show explicit no-prediction states until approval is
    EARNED via the evaluation ladder on real Liga MX data.
  - LIGAMX_SHADOW_ENABLED defaults FALSE (unlike MLS/EPL): even the
    ingest/discovery machinery stays off until an operator turns it on.
  - SPLIT SEASONS: the competition slug spans Apertura 2026 + Clausura
    2027 (the ESPN season year), with the tournament recorded on every
    data surface — see the competition_identity_decision in the
    research summary. The tournament-shaped knowledge lives in
    src/ligamx.py and here, never in the shared modules.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
from src.live import identity, ingest, markets, model_ligamx, runs
from src.live.db import get_session, plane_ready

LIGAMX_SLUG = "liga-mx-2026"
ESPN_LEAGUE = "mex.1"
ESPN_TEAMS_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                  "mex.1/teams")

# Kalshi-title -> ESPN-displayName bridges, CURATED from all 221
# archived KXLIGAMXGAME event titles + the 9 open ones (36 distinct
# sides, research_archive/ligamx_kalshi_events_full_2026-07-29.json).
# Seeded as APPROVED aliases — approval here is this curated,
# evidence-backed map, exactly as the MLS and EPL bridges were. Kalshi
# titles are ASCII while ESPN names carry accents (América, Querétaro);
# each mapping below was matched by hand against the archived titles.
# Mazatlán appeared in 25/26 titles but is RELEGATED (not an ESPN mex.1
# club) and is deliberately absent: were it ever to reappear, its
# fixtures stay visibly unmapped rather than guessed.
KALSHI_BRIDGES = {
    "America": "América",
    "CF America": "América",                 # 25/26 long form
    "Atlante": "Atlante",
    "Atlas": "Atlas",
    "Atlas FC": "Atlas",
    "Atletico San Luis": "Atlético de San Luis",
    "San Luis": "Atlético de San Luis",      # the open-event form
    "CF Cruz Azul": "Cruz Azul",
    "Cruz Azul": "Cruz Azul",
    "CD Guadalajara": "Guadalajara",
    "Guadalajara": "Guadalajara",
    "FC Juarez": "FC Juarez",
    "Juarez": "FC Juarez",
    "Club Leon": "León",
    "Leon": "León",
    "CF Monterrey": "Monterrey",
    "Monterrey": "Monterrey",
    "Club Necaxa": "Necaxa",
    "Necaxa": "Necaxa",
    "CF Pachuca": "Pachuca",
    "Pachuca": "Pachuca",
    "Club Puebla": "Puebla",
    "Puebla": "Puebla",
    "Pumas UNAM": "Pumas UNAM",
    "Queretaro": "Querétaro",
    "Queretaro FC": "Querétaro",
    "Club Santos Laguna": "Santos",
    "Santos Laguna": "Santos",
    "Tigres": "Tigres UANL",
    "Tigres UANL": "Tigres UANL",
    "Club Tijuana de Caliente": "Tijuana",
    "Tijuana de Caliente": "Tijuana",
    "Deportivo Toluca FC": "Toluca",
    "Toluca": "Toluca",
}


def _market_spec() -> markets.CompetitionMarketSpec:
    from src.ligamx import MATCH_FAMILIES, model_key_for
    game = config.LIGAMX_KALSHI_GAME_SERIES
    return markets.CompetitionMarketSpec(
        slug=LIGAMX_SLUG,
        game_series=game,
        # game first: it anchors the mapping; the rest suffix-join to it
        family_series=tuple(dict.fromkeys(
            [game] + [series for _k, series, _l in MATCH_FAMILIES
                      if series != game])),
        ticker_prefix="KXLIGAMX",
        first_half_prefix="KXLIGAMX1H",
        model_key_fn=model_key_for,
        # same predicate as mls-lock-v2 (capture-clock freshness, game
        # 3-way required); its own name so a Liga MX snapshot never
        # claims another competition's policy string
        lock_policy_version="ligamx-lock-v1")


MARKET_SPEC = _market_spec()

RUNS_SPEC = runs.CompetitionRunsSpec(
    slug=LIGAMX_SLUG, model_module=model_ligamx,
    market_spec=MARKET_SPEC, label="LigaMX", expected_teams=18,
    enabled_fn=lambda: config.LIGAMX_SHADOW_ENABLED)


# --- thin entry points (scheduler + API) -----------------------------------

def seed_teams() -> dict:
    return identity.seed_league_teams(LIGAMX_SLUG, ESPN_TEAMS_URL,
                                      KALSHI_BRIDGES)


def ingest_season() -> dict:
    return ingest.ingest_season_schedules(competition_slug=LIGAMX_SLUG,
                                          espn_league=ESPN_LEAGUE)


# ESPN's season year for Apertura 2025 + Clausura 2026 — the tournaments
# BEFORE the ones this slug is named for. One year covers both, because
# ESPN's mex.1 season year spans them (the same fact ladder_replay's
# ReplaySource records: the year cannot separate them, only the slug can).
HISTORY_SEASON_YEAR = 2025


def ingest_history() -> dict:
    """Pull the PREVIOUS season's completed fixtures into this slug.

    Why this exists: `fit()` refuses to rate a club with fewer than
    MIN_GAMES completed matches, and the Apertura that this slug is named
    for had two rounds played on 2026-07-30 — so every club sat below the
    floor and the odds board was empty while the model was correctly
    approved. Measured on ESPN that day: pinning season=2025 returns 40
    completed fixtures for Guadalajara (17 Apertura + 17 Clausura + 6
    playoff), so the floor is comfortably cleared.

    These land in liga-mx-2026 deliberately, NOT under a slug of their
    own: `Team` rows are competition-keyed, so a separate slug would mint
    a second set of team ids and the ratings fitted on them could not be
    looked up for a current-season fixture. Keeping one slug keeps one
    identity. The 90-day recency half-life then does the honest thing on
    its own — these results decay out as the new Apertura accumulates.

    The shared reasoning now lives in `ingest.ingest_prior_season` — EPL
    and La Liga hit the same wall, so the WHY is stated once there rather
    than three times."""
    return ingest.ingest_prior_season(
        competition_slug=LIGAMX_SLUG, espn_league=ESPN_LEAGUE,
        season_year=HISTORY_SEASON_YEAR)


def refresh_window() -> dict:
    return ingest.refresh_window(competition_slug=LIGAMX_SLUG,
                                 espn_league=ESPN_LEAGUE)


def discover_and_map() -> dict:
    return markets.discover_and_map(spec=MARKET_SPEC)


def capture_quotes() -> dict:
    return markets.capture_quotes(spec=MARKET_SPEC)


def scheduled_runs(freshness_hours: float = 4.0) -> dict:
    return runs.scheduled_runs(freshness_hours=freshness_hours,
                               spec=RUNS_SPEC)


def t10_locks() -> dict:
    return runs.t10_locks(spec=RUNS_SPEC)


def model_for_event(espn_event_id: str) -> dict | None:
    return runs.model_for_event(espn_event_id, spec=RUNS_SPEC)


def shadow_counts() -> dict:
    return runs.shadow_counts(spec=RUNS_SPEC)


def latest_odds() -> list[dict]:
    return runs.latest_odds(spec=RUNS_SPEC)


def empty_board_reason() -> dict:
    """Why this board is empty. Shared implementation in runs."""
    return runs.empty_board_reason(spec=RUNS_SPEC)


# --- the discovery probe + explicit unmapped state -------------------------

def discovery_status() -> dict:
    """The honest market-readiness answer: what the configured series
    ACTUALLY serves right now, plus the local mapped/unmapped state.

    The series ticker is config with a live probe, never an assertion:
    `series_exists` is Kalshi's own answer (GET /series/{ticker}), and
    `open_events` counts what the events endpoint returns today. On
    2026-07-29 that was exists=True, open_events=9 — a live market,
    reported as measured, not assumed to persist.
    """
    import requests as _rq
    game = config.LIGAMX_KALSHI_GAME_SERIES
    out: dict = {"game_series": game, "series_exists": None,
                 "open_events": None, "probed_at":
                 datetime.now(timezone.utc).isoformat()}
    try:
        r = _rq.get(f"{markets.KALSHI}/series/{game}", timeout=10)
        out["series_exists"] = bool(r.status_code == 200)
    except _rq.RequestException as exc:
        out["series_probe_error"] = str(exc)[:120]
    try:
        d = _rq.get(f"{markets.KALSHI}/events",
                    params={"series_ticker": game, "status": "open",
                            "limit": 100}, timeout=10)
        d.raise_for_status()
        out["open_events"] = len(d.json().get("events") or [])
    except _rq.RequestException as exc:
        out["events_probe_error"] = str(exc)[:120]

    if plane_ready():
        from datetime import timedelta

        from src.live.models import Fixture, MarketEvent
        s = get_session()
        try:
            out["events_recorded"] = s.query(MarketEvent).filter_by(
                competition_slug=LIGAMX_SLUG).count()
            out["mapped_events"] = s.query(MarketEvent).filter_by(
                competition_slug=LIGAMX_SLUG, mapping_approved=True).count()
            horizon = (datetime.now(timezone.utc) + timedelta(hours=48))
            upcoming = [f for f in s.query(Fixture)
                        .filter_by(competition_slug=LIGAMX_SLUG,
                                   status="pre")
                        .all()
                        if f.current_kickoff_utc is not None
                        and (f.current_kickoff_utc.replace(
                            tzinfo=f.current_kickoff_utc.tzinfo
                            or timezone.utc)) <= horizon
                        and f.home_team_id is not None
                        and f.away_team_id is not None]
            unmapped = [f.espn_event_id for f in upcoming
                        if not s.query(MarketEvent).filter_by(
                            fixture_id=f.id, mapping_approved=True).first()]
            out["upcoming_48h"] = len(upcoming)
            out["unmapped_upcoming"] = len(unmapped)
            out["unmapped_upcoming_ids"] = unmapped[:10]
        finally:
            s.close()
    else:
        out["live_plane"] = "dormant"
    return out


# --- approval status (the DARK contract, stated) ---------------------------

def approval_status() -> dict:
    """What the runtime is (not) operating under for liga-mx-2026-v0.
    LOAD-ONLY — mirrors the fail-closed boot rule (V9.5 H6): nothing
    here can create or activate an approval. Until an evaluation ladder
    run on real Liga MX data earns one, this reports the dark state and
    every odds surface stays an explicit empty."""
    out = {"model_version": model_ligamx.MODEL_NAME,
           "approved_for_shadow": False,
           "approval_decision_missing": True,
           "mode": "dark",
           "note": ("liga-mx-2026-v0 is scaffolded but UNAPPROVED: no "
                    "approval decision exists, runs and locks are "
                    "structurally refused (F3/F9), and no odds render "
                    "anywhere. The Kalshi markets are OPEN — the model "
                    "still may not run until approval is earned through "
                    "the evaluation ladder on real Liga MX data.")}
    if not plane_ready():
        out["live_plane"] = "dormant"
        return out
    from src.live.models import ModelApprovalDecision, ModelVersion
    s = get_session()
    try:
        mv = s.query(ModelVersion).filter_by(
            name=model_ligamx.MODEL_NAME).first()
        dec = (s.query(ModelApprovalDecision)
               .filter_by(model_version_name=model_ligamx.MODEL_NAME,
                          approved=True)
               .order_by(ModelApprovalDecision.id.desc()).first())
        out["model_version_registered"] = mv is not None
        out["approved_for_shadow"] = bool(mv and mv.approved_for_shadow)
        out["approval_decision_missing"] = dec is None
        if dec is not None:                      # future-proof honesty
            out["mode"] = "approved_decision_present"
            out["decision_id"] = dec.id
            out["content_hash"] = dec.content_hash
            # the dark-state note above is now stale — restate what is
            # actually true rather than leave a decision_id sitting next
            # to a sentence claiming none exists
            out["note"] = (
                f"liga-mx-2026-v0 has an active approval decision "
                f"(id {dec.id}, policy {dec.policy_version!r}) — shadow "
                f"collection and locks run once fixtures exist. This does "
                f"NOT mean real money signals are enabled "
                f"(real_money_signals stays server-side gated) or that an "
                f"edge is established.")
        return out
    finally:
        s.close()


def boot() -> dict:
    """One-shot Liga MX boot: identity -> season ingest -> market
    discovery -> DARK model registration. Each step isolated; instant
    no-op when the live plane is dormant or LIGAMX_SHADOW_ENABLED is
    off (the DEFAULT — this plane starts switched off). NEVER creates
    or activates an approval — the model stays dark."""
    if not config.LIGAMX_SHADOW_ENABLED:
        return {"skipped": "LIGAMX_SHADOW_ENABLED off"}
    results: dict = {}
    for name, step in (
        ("seed_teams", seed_teams),
        ("season_ingest", ingest_season),
        # the previous season, so the ratings have a history to stand on
        # while the new Apertura is still only a round or two old
        ("history_ingest", ingest_history),
        ("market_map", discover_and_map),
        # Registration with the TWO-ARM boot flag (#59 extended): the
        # flag survives a revision-only deploy, and everything else —
        # genuine engine change, missing decision, any error — stays
        # dark. The old unconditional force-dark cost one manual rearm
        # per deploy per plane at the operator's release cadence.
        ("model_version_flag",
         lambda: model_ligamx.ensure_model_version(
             approved_for_shadow=__import__(
                 "src.live.model_eval", fromlist=["x"]).boot_shadow_flag(
                 model_ligamx, model_ligamx.MODEL_NAME))),
    ):
        try:
            results[name] = step()
            print(f"[ligamx-boot] {name}: {results[name]}")
        except Exception as exc:
            results[name] = {"error": str(exc)[:200]}
            print(f"[ligamx-boot] {name} FAILED: {exc}")
    try:
        print(f"[ligamx-boot] approval: {approval_status()}")
    except Exception as exc:
        print(f"[ligamx-boot] approval status FAILED: {exc}")
    return results
