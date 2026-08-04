"""EPL live plane — epl-2026 entry points over the competition-keyed
machinery (2026-07-28).

Everything here is a thin, league-named binding of the SHARED modules
(identity / ingest / markets / runs), which since the EPL build take a
competition spec and default to MLS. Nothing is duplicated: the same
discovery, quote-capture and T-10 lock code serves both competitions,
keyed by slug.

State of the world this module is honest about:
  - Kalshi's KXEPLGAME series exists (387 events in 25/26) and lists NO
    2026-27 fixture as of 2026-07-28. Note what the archive actually
    shows: a `status=open` probe returned TEN events, all dated 26MAY24
    — the settled final matchday of 2025-26. "Open" is not "current" at
    this venue, so discovery_status() reports the raw provider count AND
    how many of those events fall inside the fixture horizon, instead of
    letting ten settled events read as ten live markets.
  - epl-2026-v0 is DARK: unapproved, no approval decision, so the runs
    machinery refuses every run and lock (F3/F9 fail-closed). The
    surfaces show explicit no-prediction states until approval is
    EARNED via the evaluation ladder on real season data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
from src.live import identity, ingest, markets, model_epl, runs
from src.live.db import get_session, plane_ready

EPL_SLUG = "epl-2026"
ESPN_LEAGUE = "eng.1"
ESPN_TEAMS_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                  "eng.1/teams")

# Kalshi-title -> ESPN-displayName bridges, VERIFIED against all 387
# KXEPLGAME event titles of 2025-26 (research_archive/epl/). Seeded as
# APPROVED aliases — approval here is this curated, evidence-backed map,
# exactly as the MLS bridges were. The three promoted clubs (Coventry
# City, Hull City, Ipswich Town) have NO Kalshi history and are
# deliberately absent: their fixtures stay visibly unmapped until the
# 26/27 listings reveal Kalshi's names for them.
KALSHI_BRIDGES = {
    "Arsenal": "Arsenal",
    "Aston Villa": "Aston Villa",
    "Bournemouth": "AFC Bournemouth",
    "Brentford": "Brentford",
    "Brighton": "Brighton & Hove Albion",
    "Chelsea": "Chelsea",
    "Crystal Palace": "Crystal Palace",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Leeds United": "Leeds United",
    "Liverpool": "Liverpool",
    "Man Utd": "Manchester United",          # both forms appeared in 25/26
    "Manchester United": "Manchester United",
    "Manchester City": "Manchester City",
    "Newcastle": "Newcastle United",
    "Nottingham": "Nottingham Forest",
    "Sunderland": "Sunderland",
    "Tottenham": "Tottenham Hotspur",
}


def _market_spec() -> markets.CompetitionMarketSpec:
    from src.epl import MATCH_FAMILIES, model_key_for
    game = config.EPL_KALSHI_GAME_SERIES
    return markets.CompetitionMarketSpec(
        slug=EPL_SLUG,
        game_series=game,
        # game first: it anchors the mapping; the rest suffix-join to it
        family_series=tuple(dict.fromkeys(
            [game] + [series for _k, series, _l in MATCH_FAMILIES
                      if series != game])),
        ticker_prefix="KXEPL",
        first_half_prefix="KXEPL1H",
        model_key_fn=model_key_for,
        # same predicate as mls-lock-v2 (capture-clock freshness, game
        # 3-way required); its own name so an EPL snapshot never claims
        # an MLS policy string
        lock_policy_version="epl-lock-v1")


MARKET_SPEC = _market_spec()

RUNS_SPEC = runs.CompetitionRunsSpec(
    slug=EPL_SLUG, model_module=model_epl,
    market_spec=MARKET_SPEC, label="EPL", expected_teams=20,
    enabled_fn=lambda: config.EPL_SHADOW_ENABLED)


# --- thin entry points (scheduler + API) -----------------------------------

def seed_teams() -> dict:
    return identity.seed_league_teams(EPL_SLUG, ESPN_TEAMS_URL,
                                      KALSHI_BRIDGES)


def ingest_season() -> dict:
    """Season ingest PINNED to the configured season, refusing any event
    that belongs to another one (the 2026-27 assertion)."""
    return ingest.ingest_season_schedules(
        competition_slug=EPL_SLUG, espn_league=ESPN_LEAGUE,
        expected_season_year=config.EPL_SEASON_YEAR,
        expected_season_label=config.EPL_SEASON_LABEL)


# The season before EPL_SEASON_YEAR. Measured 2026-07-30: eng.1
# season=2025 returns 38 completed fixtures per club, all carrying
# per-event season.year 2025.
HISTORY_SEASON_YEAR = config.EPL_SEASON_YEAR - 1


def ingest_history() -> dict:
    """Previous-season fixtures, so the ratings have a history to stand on.

    EPL needs this MORE than the league that exposed the problem: the
    2026-27 season has zero completed fixtures and does not start until
    2026-08-21, so without history every club is unrated on day one and
    the first matchday would price nothing."""
    return ingest.ingest_prior_season(
        competition_slug=EPL_SLUG, espn_league=ESPN_LEAGUE,
        season_year=HISTORY_SEASON_YEAR)


def refresh_window() -> dict:
    return ingest.refresh_window(competition_slug=EPL_SLUG,
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
    `series_exists` is Kalshi's own answer (GET /series/{ticker}),
    `open_events` is the raw count the events endpoint returns for
    status=open, and `open_events_in_horizon` is how many of those are
    actually near a fixture date.

    Both numbers are reported because the first one lies. Archived
    2026-07-28: status=open returned TEN KXEPLGAME events and every one
    was dated 26MAY24 — the settled last matchday of 2025-26. Reporting
    only `open_events` would have said "10 open EPL markets" about a
    series with no current listings at all.
    """
    import requests as _rq
    from src.epl import MATCH_FAMILIES
    game = config.EPL_KALSHI_GAME_SERIES
    out: dict = {"game_series": game, "series_exists": None,
                 "open_events": None,
                 "season": config.EPL_SEASON_LABEL,
                 # the caveat travels WITH the data: these families' 26/27
                 # listing behaviour and ticker-tail grammar have never
                 # been observed, only their series definitions
                 "family_grammar_status": config.EPL_FAMILY_GRAMMAR_STATUS,
                 "unverified_families": [sr for _k, sr, _l in MATCH_FAMILIES
                                         if sr != game],
                 "probed_at":
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
        evs = d.json().get("events") or []
        out["open_events"] = len(evs)
        from src.epl import events_in_horizon
        out["open_events_in_horizon"] = len(events_in_horizon(evs))
        out["open_status_note"] = (
            "status=open is not status=current at this venue — the "
            "2026-07-28 archive shows 10 'open' KXEPLGAME events, all "
            "settled 2025-26 fixtures")
    except _rq.RequestException as exc:
        out["events_probe_error"] = str(exc)[:120]

    if plane_ready():
        from datetime import timedelta

        from src.live.models import Fixture, MarketEvent
        s = get_session()
        try:
            out["events_recorded"] = s.query(MarketEvent).filter_by(
                competition_slug=EPL_SLUG).count()
            out["mapped_events"] = s.query(MarketEvent).filter_by(
                competition_slug=EPL_SLUG, mapping_approved=True).count()
            horizon = (datetime.now(timezone.utc) + timedelta(hours=48))
            upcoming = [f for f in s.query(Fixture)
                        .filter_by(competition_slug=EPL_SLUG, status="pre")
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
    """What the runtime is (not) operating under for epl-2026-v0.
    LOAD-ONLY — mirrors the fail-closed boot rule (V9.5 H6): nothing
    here can create or activate an approval. Until an evaluation ladder
    run on real 2026-27 data earns one, this reports the dark state and
    every odds surface stays an explicit empty."""
    out = {"model_version": model_epl.MODEL_NAME,
           "approved_for_shadow": False,
           "approval_decision_missing": True,
           "mode": "dark",
           "note": ("epl-2026-v0 is scaffolded but UNAPPROVED: no "
                    "approval decision exists, runs and locks are "
                    "structurally refused (F3/F9), and no odds render "
                    "anywhere. Approval must be earned through the "
                    "evaluation ladder once 2026-27 data exists.")}
    if not plane_ready():
        out["live_plane"] = "dormant"
        return out
    from src.live.models import ModelApprovalDecision, ModelVersion
    s = get_session()
    try:
        mv = s.query(ModelVersion).filter_by(
            name=model_epl.MODEL_NAME).first()
        dec = (s.query(ModelApprovalDecision)
               .filter_by(model_version_name=model_epl.MODEL_NAME,
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
                f"epl-2026-v0 has an active approval decision "
                f"(id {dec.id}, policy {dec.policy_version!r}) — shadow "
                f"collection and locks run once fixtures exist. This does "
                f"NOT mean real money signals are enabled "
                f"(real_money_signals stays server-side gated) or that an "
                f"edge is established.")
        return out
    finally:
        s.close()


def boot() -> dict:
    """One-shot EPL boot: identity -> season ingest -> market discovery
    -> DARK model registration. Each step isolated; instant no-op when
    the live plane is dormant or EPL_SHADOW_ENABLED is off. NEVER
    creates or activates an approval — the model stays dark."""
    if not config.EPL_SHADOW_ENABLED:
        return {"skipped": "EPL_SHADOW_ENABLED off"}
    results: dict = {}
    for name, step in (
        ("seed_teams", seed_teams),
        ("season_ingest", ingest_season),
        # the previous season, or every club is unrated on matchday one
        ("history_ingest", ingest_history),
        ("market_map", discover_and_map),
        # Registration with the TWO-ARM boot flag (#59 extended): the
        # flag survives a revision-only deploy, and everything else —
        # genuine engine change, missing decision, any error — stays
        # dark. The old unconditional force-dark cost one manual rearm
        # per deploy per plane at the operator's release cadence.
        ("model_version_flag",
         lambda: model_epl.ensure_model_version(
             approved_for_shadow=__import__(
                 "src.live.model_eval", fromlist=["x"]).boot_shadow_flag(
                 model_epl, model_epl.MODEL_NAME))),
    ):
        try:
            results[name] = step()
            print(f"[epl-boot] {name}: {results[name]}")
        except Exception as exc:
            results[name] = {"error": str(exc)[:200]}
            print(f"[epl-boot] {name} FAILED: {exc}")
    try:
        print(f"[epl-boot] approval: {approval_status()}")
    except Exception as exc:
        print(f"[epl-boot] approval status FAILED: {exc}")
    return results
