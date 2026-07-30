"""Background jobs.

  poll_odds          : every ODDS_POLL_SECONDS (default 30s), record an
                       OddsReading for every open market on matches within
                       the prediction window — this is the learning corpus —
                       then score every watched market and fire a ripeness
                       alert the moment one crosses the threshold.
  hourly_predictions : every hour, re-simulate every match kicking off within
                       the configured window and refresh suggestions.
  final_lock_check   : every minute, lock a FINAL decision exactly once when a
                       match is <= 10 minutes from kickoff.
"""
from __future__ import annotations

from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

import config
from src.alerts import (OPERATIONAL, alert_final_lock, alert_new_take,
                        alert_ripe, send_alert)
from src.bracket import resolve_bracket
from src.db import SessionLocal, WatchlistItem, utcnow
from src.kalshi_client import KalshiClient
from src.live_signals import evaluate_live_signals
from src.model_cache import get_model_prob, refresh_model_cache
from src.schedule_data import is_trackable, load_schedule
from src import spike_detector
from src import bots
from src import live_state
from src.suggester import SuggesterEngine
from src.timing import compute_timing, record_reading, save_alert, should_alert

engine = SuggesterEngine()
kalshi = KalshiClient()
_finalized: set[str] = set()          # match_ids already locked this process


def hourly_predictions() -> None:
    now = utcnow()
    for match in load_schedule():
        if not is_trackable(match, now, config.HOURLY_PREDICTION_WINDOW_HOURS,
                            config.TRACK_HOURS_AFTER_KICKOFF):
            continue
        try:
            result = engine.run_for_match(match, source="scheduled")
        except Exception as exc:  # one bad match must never kill the batch
            print(f"[hourly] {match.match_id} FAILED: {exc}")
            continue
        refresh_model_cache(result)
        takes = [s for s in result["suggestions"] if s["recommendation"] == "TAKE"]
        print(f"[hourly] {match.match_id}: {len(takes)} TAKE / "
              f"{len(result['suggestions'])} markets")
        for s in takes[:3]:  # don't spam Discord
            alert_new_take(f"{match.home} vs {match.away}",
                           s["market_title"], s["edge"], s["expected_value"])


def live_tick() -> None:
    """Refresh live match-state snapshots and freeze finished matches.

    Its OWN fast job, decoupled from poll_odds: that job spends minutes on
    per-event Kalshi fetches, and while it runs APScheduler skips further
    fires (max_instances=1) — riding inside it degraded the live scoreboard
    to one update per ~2 minutes. This tick is one cached feed pull + a
    snapshot upsert, so it comfortably runs every LIVE_TICK_SECONDS."""
    try:
        r = live_state.poll_live_state()
        if r["frozen"]:
            print(f"[live-state] froze {r['frozen']} finished match(es)")
    except Exception as exc:
        print(f"[live-state] poll error: {exc}")


def poll_odds() -> None:
    """The always-on heartbeat: record every market's price, then check
    whether any watched bet just became ripe."""
    now = utcnow()
    matches = [m for m in load_schedule()
               if is_trackable(m, now, config.HOURLY_PREDICTION_WINDOW_HOURS,
                               config.TRACK_HOURS_AFTER_KICKOFF)]
    if not matches:
        return

    with SessionLocal() as session:
        watched = {w.market_id: w for w in
                   session.execute(select(WatchlistItem)).scalars().all()}

    for match in matches:
        try:
            mkts = kalshi.get_markets_for_match(match)
            # Layer 1 (LOG-ONLY): infer goals from the scoreline
            # distribution. Wrapped separately so a detector hiccup can
            # never disturb polling, and it touches nothing downstream.
            try:
                spike_detector.inspect(match.match_id, mkts)
            except Exception as exc:
                print(f"[spike] {match.match_id} detector error: {exc}")

            for mkt in mkts:
                record_reading(match.match_id, mkt,
                               get_model_prob(mkt["market_id"]))

                item = watched.get(mkt["market_id"])
                if not item:
                    continue
                timing = compute_timing(mkt["market_id"], match.kickoff)
                if should_alert(mkt["market_id"], timing):
                    save_alert(match.match_id, mkt["market_id"], mkt["title"],
                               timing)
                    alert_ripe(f"{match.home} vs {match.away}", mkt["title"],
                               timing)
                    print(f"[RIPE {timing['score']:.0f}] {mkt['title']}")
        except Exception as exc:  # keep polling the other matches
            print(f"[poll] {match.match_id} FAILED: {exc}")
            continue


def final_lock_check() -> None:
    now = utcnow()
    lock_delta = timedelta(minutes=config.FINAL_LOCK_MINUTES_BEFORE_KICKOFF)
    for match in load_schedule():
        if match.match_id in _finalized:
            continue
        time_left = match.kickoff - now
        if timedelta(0) < time_left <= lock_delta:
            try:
                result = engine.run_for_match(match, source="final_lock",
                                              is_final=True)
            except Exception as exc:  # retry on the next minute tick
                print(f"[final_lock] {match.match_id} FAILED: {exc}")
                continue
            refresh_model_cache(result)
            _finalized.add(match.match_id)
            takes = [s for s in result["suggestions"] if s["recommendation"] == "TAKE"]
            best = takes[0] if takes else None
            alert_final_lock(f"{match.home} vs {match.away}", best)
            print(f"[FINAL LOCK] {match.match_id} locked at T-{time_left}")


def live_signals_job() -> None:
    """BUY/SELL reads on WATCHED markets during live play. Piggybacks on the
    same ~25s-cached live_auto cycle the frontend stream reads, so a pass is
    nearly free; the module itself handles thresholds, cooldowns and pushes."""
    try:
        r = evaluate_live_signals(engine)
        if r["fired"]:
            print(f"[live-signals] fired {r['fired']} "
                  f"(checked {r['checked']} watched markets)")
    except Exception as exc:
        print(f"[live-signals] pass error: {exc}")


def bots_job() -> None:
    """The strategy-lab bots' pass: entries, exits, settlements. Rides the
    same cached prediction/live cycles everything else reads."""
    try:
        from src.bots import bots_tick
        r = bots_tick(engine)
        if r["opened"] or r["closed"] or r["settled"]:
            print(f"[bots] tick: {r['opened']} opened, {r['closed']} closed, "
                  f"{r['settled']} settled")
    except Exception as exc:
        print(f"[bots] tick error: {exc}")


def resolve_bracket_job() -> None:
    """Fill QF placeholder slots as R16 results land (fixtures only; team
    stats stay hand-sourced). Cheap and idempotent: does nothing once the
    bracket is fully known, so this can run often without burning feed budget.
    Announces each newly-decided matchup to Discord once."""
    try:
        changed = resolve_bracket()
    except Exception as exc:  # never let bracket work disturb the scheduler
        print(f"[bracket] resolve FAILED: {exc}")
        return
    # OPERATIONAL, not a betting signal: this announces a FIXTURE FACT
    # already settled by a played match — no model output, no price, no
    # market view, nothing that says a contract is mispriced. It rides
    # the gate like everything else so the classification is recorded at
    # the call site rather than assumed.
    for c in changed:
        send_alert(
            f"🗓️ Quarter-final set: **{c['team']}** advances into "
            f"{c['qf']} ({c['side']}).",
            title="Bracket", dispatch_class=OPERATIONAL)


def boot_sequence() -> None:
    """Ordered boot recovery + prime — ONE job, because these raced as four
    independent one-shots. If the prediction prime ran before
    restore_missing_results had re-frozen finished results (the DB is wiped
    on every deploy) and the bracket resolver had filled the next round's
    slots, unresolved matches were skipped by the prime and the board sat on
    placeholder default-stats numbers until the next hourly cron (observed
    on prod 2026-07-12: SF2 served xg 1.398/1.398, advance ~0.50).

    The order is load-bearing:
      1. restore results   — bracket resolution reads frozen MatchResults
                             (the feed fallback can't fetch finished 2026
                             fixtures on the free plan);
      2. resolve bracket   — priming needs real team names in the slots;
      3. prime predictions — the odds poller needs model probs for edge;
      4. prime odds poll.
    Steps are isolated: a failing restore (ESPN down) must not leave the
    bracket unresolved or the dashboard unprimed."""
    for name, step in (
        ("restore_results", live_state.restore_missing_results),
        ("restore_ledger", bots.restore_from_archive),
        ("resolve_bracket", resolve_bracket_job),
        ("prime_predictions", hourly_predictions),
        ("prime_poll", poll_odds),
    ):
        try:
            step()
        except Exception as exc:
            print(f"[boot] {name} FAILED: {exc}")


# --- MLS shadow plane (launch decision: shadow mode, money locked) ---------
# Lazy imports inside each job: an import failure in the live plane must
# never take the scheduler (and with it the WC26 archive) down.

def mls_boot() -> None:
    """One-shot live-plane boot: identity -> season history -> market map
    -> first shadow runs -> walk-forward validation. Each step isolated;
    all no-op instantly when the live DB is dormant."""
    if not config.MLS_SHADOW_ENABLED:
        return
    steps = (
        ("seed_teams", lambda: __import__(
            "src.live.identity", fromlist=["x"]).seed_teams()),
        ("season_ingest", lambda: __import__(
            "src.live.ingest", fromlist=["x"]).ingest_season_schedules()),
        # official Sportec team stats (xG) — AFTER fixtures exist (they're
        # the attach target) and BEFORE approval/runs, so the first
        # evaluation + shadow runs see xG. skip_existing makes re-boots
        # cheap: only newly-completed matches are fetched. team-only keeps
        # boot bounded; the rolling job below adds players + freshness.
        ("stats_backfill", lambda: __import__(
            "src.live.mls_stats", fromlist=["x"]).ingest_match_stats(
                with_players=False, skip_existing=True)),
        # ESPN<->Sportec player id bridge from per-match participants
        # (99.5% on starters). No-op until player rows exist; skip_covered
        # keeps re-boots cheap. Additive identity — no model effect.
        ("player_bridge", lambda: __import__(
            "src.live.player_bridge", fromlist=["x"]).build_bridge()),
        ("market_map", lambda: __import__(
            "src.live.markets", fromlist=["x"]).discover_and_map()),
    )
    for name, step in steps:
        try:
            print(f"[mls-boot] {name}: {step()}")
        except Exception as exc:
            print(f"[mls-boot] {name} FAILED: {exc}")
    # approval BEFORE any run: scheduled_runs/t10_locks enforce the
    # approved_for_shadow gate, so approval must be (re-)earned first.
    # V9 eval F1: this is the CONFIDENCE-INTERVAL evaluator + an IMMUTABLE
    # persisted decision record — no longer a bare Monte-Carlo point
    # estimate. approved_for_shadow is set FROM that decision.
    try:
        from src.live import model_eval
        # V9.5 eval H6: LOAD only. Boot must never mint an approval for
        # itself — the engine signature includes code_revision, so every
        # deploy was quietly issuing a fresh approval computed from
        # whatever the mutable database held at that moment, while the
        # governance claim was that re-evaluation is an explicit
        # operator action. No active decision => model stays unapproved
        # => canonical locks are structurally refused. Fail closed.
        dec = model_eval.ensure_approval_decision(allow_create=False)
        print(f"[mls-boot] approval decision: {dec}")
    except Exception as exc:
        print(f"[mls-boot] approval FAILED: {exc}")
    try:
        from src.live import runs as live_runs
        print(f"[mls-boot] shadow_runs: {live_runs.scheduled_runs()}")
    except Exception as exc:
        print(f"[mls-boot] shadow_runs FAILED: {exc}")


def storage_headroom_job() -> None:
    """Watch the live volume. Railway's own alerts are Teams/Pro-only, so
    without this nothing warns before the disk fills — and a full volume
    fails every prediction write SILENTLY behind {"created": 0}, which is
    exactly what happened on 2026-07-25."""
    try:
        from src.live import observability
        r = observability.check_storage_headroom()
        if r.get("dormant"):
            return
        if r.get("alerted"):
            print(f"[storage] ALERTED at {r['used_pct']}% of volume")
        elif r.get("over_threshold"):
            print(f"[storage] over threshold ({r['used_pct']}%), "
                  f"alert suppressed: {r.get('suppressed')}")
        else:
            print(f"[storage] {r.get('used_pct')}% of volume used")
    except Exception as exc:
        print(f"[storage] error: {exc}")


def mls_window_job() -> None:
    """Rolling fixture refresh: reschedules, status flips, final scores;
    then settle any paper fills whose fixtures just completed."""
    try:
        from src.live import ingest
        ingest.refresh_window()
    except Exception as exc:
        print(f"[mls-window] error: {exc}")
    try:
        from src.live import paper
        r = paper.settle_paper()
        if r.get("settled"):
            print(f"[mls-paper] settled {r['settled']} fills")
    except Exception as exc:
        print(f"[mls-paper] settle error: {exc}")


def mls_markets_job() -> None:
    """Kalshi discovery/mapping + full-book capture for the horizon."""
    try:
        from src.live import markets
        markets.discover_and_map()
        markets.capture_quotes()
    except Exception as exc:
        print(f"[mls-markets] error: {exc}")


def mls_runs_job() -> None:
    """Fresh shadow odds for every upcoming fixture in the horizon."""
    try:
        from src.live import runs
        r = runs.scheduled_runs()
        if r.get("created"):
            print(f"[mls-runs] {r}")
    except Exception as exc:
        print(f"[mls-runs] error: {exc}")


def mls_stats_job() -> None:
    """Rolling refresh of official Sportec team + player stats for recently
    completed matches (xG substrate for the model, player rows for future
    GK/availability features). Cheap: only the last ~2 weeks, re-fetched so
    provisional stats correct themselves. Instant no-op when dormant."""
    try:
        from src.live import mls_stats
        r = mls_stats.ingest_match_stats(days_back=16, with_players=True)
        if r.get("ingested"):
            print(f"[mls-stats] {r.get('ingested')} matches, "
                  f"{r.get('player_rows')} player rows")
    except Exception as exc:
        print(f"[mls-stats] error: {exc}")
    # extend the ESPN<->Sportec bridge to any newly-seen players (cheap:
    # skip_covered fetches only matches with an unmapped participant)
    try:
        from src.live import player_bridge
        b = player_bridge.build_bridge()
        if b.get("newly_mapped"):
            print(f"[mls-bridge] mapped {b['newly_mapped']} players")
    except Exception as exc:
        print(f"[mls-bridge] error: {exc}")


# --- La Liga shadow plane — BEGIN additive block ---------------------------
# Same lazy-import isolation as the MLS jobs. Every job is an instant
# no-op while LALIGA_SHADOW_ENABLED is off (the default) or the live DB
# is dormant. The model is DARK — the runs/t10 jobs exist so the
# pipeline is complete the day an operator enables the flag AND a
# La Liga ladder earns an approval; until then they refuse at the
# per-model gates.

def laliga_boot() -> None:
    try:
        from src.live import laliga_live
        laliga_live.boot()
    except Exception as exc:
        print(f"[laliga-boot] error: {exc}")


def laliga_window_job() -> None:
    """Rolling La Liga fixture refresh (reschedules, statuses, scores)."""
    if not config.LALIGA_SHADOW_ENABLED:
        return
    try:
        from src.live import laliga_live
        laliga_live.refresh_window()
    except Exception as exc:
        print(f"[laliga-window] error: {exc}")


def laliga_markets_job() -> None:
    """Kalshi discovery/mapping + quote capture for La Liga."""
    if not config.LALIGA_SHADOW_ENABLED:
        return
    try:
        from src.live import laliga_live
        laliga_live.discover_and_map()
        laliga_live.capture_quotes()
    except Exception as exc:
        print(f"[laliga-markets] error: {exc}")


def laliga_runs_job() -> None:
    if not config.LALIGA_SHADOW_ENABLED:
        return
    try:
        from src.live import laliga_live
        r = laliga_live.scheduled_runs()
        if r.get("created"):
            print(f"[laliga-runs] {r}")
    except Exception as exc:
        print(f"[laliga-runs] error: {exc}")


def laliga_t10_job() -> None:
    if not config.LALIGA_SHADOW_ENABLED:
        return
    try:
        from src.live import laliga_live
        r = laliga_live.t10_locks()
        if r.get("locked"):
            print(f"[laliga-t10] {r}")
    except Exception as exc:
        print(f"[laliga-t10] error: {exc}")
# --- La Liga shadow plane — END additive block -----------------------------


# --- readiness watch -----------------------------------------------------
# Since the V9.5 remediations, boot FAILS CLOSED on approval: a deploy
# leaves the model unapproved and shadow runs refused until an operator
# calls /approval/activate. That is deliberate.
#
# The gap it opens: if whoever deployed does not follow through — a
# session that pushed and then died, a deploy nobody was watching — the
# shadow plane simply stops collecting, and NOTHING says so. Every
# existing alert fires on MLS events (locks, fills), so silence is
# indistinguishable from a quiet evening. The DiskFull incident failed
# exactly this way: silently, behind a response that looked fine.
#
# So this watches the one thing no other alert covers. Process-local
# state is correct here: a fresh container restarts the clock, which is
# what we want after a legitimate deploy.
_unapproved_since: object = None
_unapproved_alerted = False
UNAPPROVED_ALERT_AFTER_S = 600      # a normal deploy + reactivation is ~2 min


def mls_readiness_watch() -> None:
    """Alert when shadow collection has stopped and nobody noticed."""
    global _unapproved_since, _unapproved_alerted
    try:
        from datetime import datetime, timezone

        from src.live.db import plane_ready
        if not plane_ready():
            return
        from src.live import model_mls
        from src.live.db import get_session
        from src.live.models import ModelVersion
        s = get_session()
        try:
            mv = (s.query(ModelVersion)
                  .filter_by(name=model_mls.MODEL_NAME).first())
            approved = bool(mv and mv.approved_for_shadow)
        finally:
            s.close()
        now = datetime.now(timezone.utc)

        if approved:
            # recovered — say so, but only if we complained first
            if _unapproved_alerted:
                # OPERATIONAL: a statement about the PLATFORM's
                # collection state. No model output, no market view —
                # and silencing it under the money lock would restore
                # exactly the invisible-halt failure this watch exists
                # to prevent.
                from src.alerts import OPERATIONAL, send_alert
                send_alert("✅ shadow collection RESUMED — model approved "
                           "again, locks will be taken normally",
                           title="Trivela readiness",
                           dispatch_class=OPERATIONAL)
            _unapproved_since = None
            _unapproved_alerted = False
            return

        if _unapproved_since is None:
            _unapproved_since = now
            return
        stalled = (now - _unapproved_since).total_seconds()
        if stalled >= UNAPPROVED_ALERT_AFTER_S and not _unapproved_alerted:
            from src.alerts import OPERATIONAL, send_alert
            mins = int(stalled // 60)
            send_alert(
                f"🛑 SHADOW COLLECTION STOPPED — the model has been "
                f"unapproved for {mins} min. A deploy invalidates the "
                f"approval and it stays invalid until someone calls "
                f"POST /api/admin/mls/approval/activate. No T-10 locks "
                f"are being taken until then.",
                title="Trivela readiness", dispatch_class=OPERATIONAL)
            _unapproved_alerted = True
    except Exception as exc:
        print(f"[mls-readiness] watch error: {exc}")


# --- Kalshi market hunter (observational scanner; shadow mode) -------------
# Scans the soccer GAME-series taxonomy for structurally mispriced books
# and records findings. OBSERVATIONAL ONLY: no order path, no advice;
# alerts (rule-based src.alerts path) state arithmetic, never imperatives.
# Lazy import + instant no-op when disabled or the live DB is dormant,
# like every other live-plane job.

def hunter_job() -> None:
    """One hunter scan cycle. The cycle row it writes is the heartbeat:
    a scanner that dies is visible as dead (stale last-cycle age on
    /api/hunter/findings), never as a quiet market."""
    try:
        from src.live import hunter
        r = hunter.scan_cycle()
        if r.get("findings_new") or r.get("findings_expired") \
                or r.get("error") or ("status" in r
                                      and r["status"] != "complete"):
            print(f"[hunter] {r}")
    except Exception as exc:
        print(f"[hunter] cycle error: {exc}")


def mls_t10_job() -> None:
    """The atomic T-10 lock sweep (book freeze + canonical run)."""
    try:
        from src.live import runs
        r = runs.t10_locks()
        if r.get("locked"):
            print(f"[mls-t10] {r}")
    except Exception as exc:
        print(f"[mls-t10] error: {exc}")


# === EPL shadow plane (additive block, 2026-07-28) =========================
# Machinery parity with MLS, MODEL DARK: the run/lock jobs are wired but
# refuse at the F3/F9 approval gates until epl-2026-v0 earns an approval
# decision — which no code path here can create. Lazy imports as with
# MLS: an EPL-plane failure must never take the scheduler down.

def epl_boot() -> None:
    """One-shot EPL boot: identity -> season ingest -> market discovery
    -> DARK model registration. No approval is ever created."""
    if not config.EPL_SHADOW_ENABLED:
        return
    try:
        from src.live import epl_plane
        epl_plane.boot()
    except Exception as exc:
        print(f"[epl-boot] FAILED: {exc}")


def epl_window_job() -> None:
    """Rolling EPL fixture refresh (reschedules, statuses, scores)."""
    if not config.EPL_SHADOW_ENABLED:
        return
    try:
        from src.live import epl_plane
        epl_plane.refresh_window()
    except Exception as exc:
        print(f"[epl-window] error: {exc}")


def epl_markets_job() -> None:
    """Kalshi discovery/mapping + quote capture for EPL. Cheap while
    the 26/27 listings are absent (empty event lists); the shared
    per-request throttle in src.live.markets covers both leagues."""
    if not config.EPL_SHADOW_ENABLED:
        return
    try:
        from src.live import epl_plane
        epl_plane.discover_and_map()
        epl_plane.capture_quotes()
    except Exception as exc:
        print(f"[epl-markets] error: {exc}")


def epl_runs_job() -> None:
    """EPL shadow-run sweep. Refuses at the approval gate while the
    model is dark — wired now so approval, once earned, needs no code
    change to start collecting."""
    if not config.EPL_SHADOW_ENABLED:
        return
    try:
        from src.live import epl_plane
        r = epl_plane.scheduled_runs()
        if r.get("created"):
            print(f"[epl-runs] {r}")
    except Exception as exc:
        print(f"[epl-runs] error: {exc}")


def epl_t10_job() -> None:
    """EPL T-10 lock sweep — same fail-closed gates as the run sweep."""
    if not config.EPL_SHADOW_ENABLED:
        return
    try:
        from src.live import epl_plane
        r = epl_plane.t10_locks()
        if r.get("locked"):
            print(f"[epl-t10] {r}")
    except Exception as exc:
        print(f"[epl-t10] error: {exc}")
# === end EPL block =========================================================


# === Liga MX shadow plane (additive block, 2026-07-29) =====================
# Machinery parity with MLS/EPL, MODEL DARK and the plane switch OFF BY
# DEFAULT (LIGAMX_SHADOW_ENABLED=false): every job below is a no-op
# until an operator flips the flag, and the run/lock jobs additionally
# refuse at the F3/F9 approval gates — which no code path here can
# satisfy. Lazy imports as with MLS: a Liga MX-plane failure must never
# take the scheduler down.

def ligamx_boot() -> None:
    """One-shot Liga MX boot: identity -> season ingest -> market
    discovery -> DARK model registration. No approval is ever created."""
    if not config.LIGAMX_SHADOW_ENABLED:
        return
    try:
        from src.live import ligamx_plane
        ligamx_plane.boot()
    except Exception as exc:
        print(f"[ligamx-boot] FAILED: {exc}")


def ligamx_window_job() -> None:
    """Rolling Liga MX fixture refresh (reschedules, statuses, scores)."""
    if not config.LIGAMX_SHADOW_ENABLED:
        return
    try:
        from src.live import ligamx_plane
        ligamx_plane.refresh_window()
    except Exception as exc:
        print(f"[ligamx-window] error: {exc}")


def ligamx_markets_job() -> None:
    """Kalshi discovery/mapping + quote capture for Liga MX. The
    listings are OPEN (unlike EPL's at build time); the shared
    per-request throttle in src.live.markets covers every league."""
    if not config.LIGAMX_SHADOW_ENABLED:
        return
    try:
        from src.live import ligamx_plane
        ligamx_plane.discover_and_map()
        ligamx_plane.capture_quotes()
    except Exception as exc:
        print(f"[ligamx-markets] error: {exc}")


def ligamx_runs_job() -> None:
    """Liga MX shadow-run sweep. Refuses at the approval gate while the
    model is dark — wired now so approval, once earned, needs no code
    change to start collecting."""
    if not config.LIGAMX_SHADOW_ENABLED:
        return
    try:
        from src.live import ligamx_plane
        r = ligamx_plane.scheduled_runs()
        if r.get("created"):
            print(f"[ligamx-runs] {r}")
    except Exception as exc:
        print(f"[ligamx-runs] error: {exc}")


def ligamx_t10_job() -> None:
    """Liga MX T-10 lock sweep — same fail-closed gates as the run
    sweep."""
    if not config.LIGAMX_SHADOW_ENABLED:
        return
    try:
        from src.live import ligamx_plane
        r = ligamx_plane.t10_locks()
        if r.get("locked"):
            print(f"[ligamx-t10] {r}")
    except Exception as exc:
        print(f"[ligamx-t10] error: {exc}")
# === end Liga MX block =====================================================


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(hourly_predictions, "cron", minute=0, id="hourly")
    scheduler.add_job(final_lock_check, "cron", second=0, id="final_lock")
    scheduler.add_job(poll_odds, "interval",
                      seconds=config.ODDS_POLL_SECONDS, id="odds_poll")
    # Live scoreboard freshness: a fast, cheap tick of its own. coalesce
    # collapses any missed fires into one; max_instances guards overlap.
    scheduler.add_job(live_tick, "interval",
                      seconds=config.LIVE_TICK_SECONDS, id="live_tick",
                      coalesce=True, max_instances=1)
    # Watched-market BUY/SELL signals: instant no-op when nothing is both
    # live and watched, otherwise rides the cached live-read cycle.
    scheduler.add_job(live_signals_job, "interval",
                      seconds=config.LIVE_SIGNAL_POLL_SECONDS,
                      id="live_signals", coalesce=True, max_instances=1)
    # Paper-trading bots: instant no-op with no trackable matches, cheap
    # otherwise (cached predictions + cached live cycle + signal rows).
    scheduler.add_job(bots_job, "interval", seconds=60,
                      id="bots", coalesce=True, max_instances=1)
    # Bracket resolution: low frequency (the bracket changes at most a handful
    # of times all tournament) and self-skipping once fully known, so it's
    # nearly free. boot_sequence covers the boot-time resolve.
    scheduler.add_job(resolve_bracket_job, "interval",
                      minutes=config.BRACKET_RESOLVE_MINUTES, id="bracket")
    scheduler.start()
    # One-shot at boot, ORDERED: restore wiped results -> resolve bracket
    # slots -> prime predictions -> prime the odds poll. A single chained
    # job — as independent one-shots these raced each other (see
    # boot_sequence docstring).
    scheduler.add_job(boot_sequence, "date", id="boot_sequence")
    # MLS shadow plane: registered unconditionally, every job no-ops
    # instantly when MLS_SHADOW_ENABLED is off or the live DB is dormant.
    scheduler.add_job(mls_window_job, "interval", minutes=15,
                      id="mls_window", coalesce=True, max_instances=1)
    scheduler.add_job(storage_headroom_job, "interval", minutes=60,
                      id="storage_headroom", coalesce=True, max_instances=1)
    scheduler.add_job(mls_markets_job, "interval", minutes=10,
                      id="mls_markets", coalesce=True, max_instances=1)
    scheduler.add_job(mls_runs_job, "interval", minutes=15,
                      id="mls_runs", coalesce=True, max_instances=1)
    # Official Sportec stats refresh: low frequency (stats only change when
    # matches complete), throttled + idempotent. Its own cadence so a slow
    # external fetch never delays the fixture/market/run jobs.
    scheduler.add_job(mls_stats_job, "interval", minutes=180,
                      id="mls_stats", coalesce=True, max_instances=1)
    scheduler.add_job(mls_t10_job, "interval", seconds=60,
                      id="mls_t10", coalesce=True, max_instances=1)
    # Kalshi market hunter: observational soccer-wide scan. Cadence is
    # config (HUNTER_POLL_MINUTES); coalesce + max_instances=1 because a
    # cycle can span several rate-limited provider pages.
    scheduler.add_job(hunter_job, "interval",
                      minutes=config.HUNTER_POLL_MINUTES,
                      id="hunter", coalesce=True, max_instances=1)
    # The one thing no other alert covers: shadow collection stopped and
    # nobody noticed. Every other alert fires on MLS events, so silence
    # reads as a quiet evening rather than as a halt.
    scheduler.add_job(mls_readiness_watch, "interval", minutes=2,
                      id="mls_readiness", coalesce=True, max_instances=1)
    # Live-plane boot is its OWN one-shot, never chained into the archive
    # boot_sequence: a live failure must not delay or break the archive.
    scheduler.add_job(mls_boot, "date", id="mls_boot")
    # === EPL shadow plane (additive block, 2026-07-28) ====================
    # Same registration pattern as MLS: unconditional add, instant no-op
    # when EPL_SHADOW_ENABLED is off or the live DB is dormant. Run/lock
    # sweeps additionally refuse at the approval gates (model dark).
    scheduler.add_job(epl_window_job, "interval", minutes=15,
                      id="epl_window", coalesce=True, max_instances=1)
    scheduler.add_job(epl_markets_job, "interval",
                      minutes=config.EPL_MARKETS_JOB_MINUTES,
                      id="epl_markets", coalesce=True, max_instances=1)
    scheduler.add_job(epl_runs_job, "interval", minutes=15,
                      id="epl_runs", coalesce=True, max_instances=1)
    scheduler.add_job(epl_t10_job, "interval", seconds=60,
                      id="epl_t10", coalesce=True, max_instances=1)
    scheduler.add_job(epl_boot, "date", id="epl_boot")
    # === end EPL block ====================================================
    # === Liga MX shadow plane (additive block, 2026-07-29) ================
    # Same registration pattern as MLS/EPL: unconditional add, instant
    # no-op while LIGAMX_SHADOW_ENABLED is off (the default) or the live
    # DB is dormant. Run/lock sweeps additionally refuse at the approval
    # gates (model dark).
    scheduler.add_job(ligamx_window_job, "interval", minutes=15,
                      id="ligamx_window", coalesce=True, max_instances=1)
    scheduler.add_job(ligamx_markets_job, "interval",
                      minutes=config.LIGAMX_MARKETS_JOB_MINUTES,
                      id="ligamx_markets", coalesce=True, max_instances=1)
    scheduler.add_job(ligamx_runs_job, "interval", minutes=15,
                      id="ligamx_runs", coalesce=True, max_instances=1)
    scheduler.add_job(ligamx_t10_job, "interval", seconds=60,
                      id="ligamx_t10", coalesce=True, max_instances=1)
    scheduler.add_job(ligamx_boot, "date", id="ligamx_boot")
    # === end Liga MX block ================================================
    # --- La Liga — BEGIN additive block ------------------------------------
    # Registered unconditionally like the MLS jobs; each is an instant
    # no-op while LALIGA_SHADOW_ENABLED is off (the default).
    scheduler.add_job(laliga_window_job, "interval", minutes=15,
                      id="laliga_window", coalesce=True, max_instances=1)
    scheduler.add_job(laliga_markets_job, "interval", minutes=10,
                      id="laliga_markets", coalesce=True, max_instances=1)
    scheduler.add_job(laliga_runs_job, "interval", minutes=15,
                      id="laliga_runs", coalesce=True, max_instances=1)
    scheduler.add_job(laliga_t10_job, "interval", seconds=60,
                      id="laliga_t10", coalesce=True, max_instances=1)
    scheduler.add_job(laliga_boot, "date", id="laliga_boot")
    # --- La Liga — END additive block ----------------------------------------
    return scheduler
