"""Operational observability (V8.1 evaluation Phase 10).

A machine-readable metrics surface for the MLS shadow plane: data
freshness, lock success, missed locks, market coverage, paper P&L, and
scheduler health. Served at /api/mls/metrics for an operator or a
monitor to alert on — the numbers the review asked to track (quote age,
missing families, canonical-lock success rate, missed locks, settlement
lag). Read-only aggregation; never mutates.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.live import competitions
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, MarketEvent, MarketSnapshot,
                             PaperFill, PaperSignal, PredictionRun)


def _now():
    return datetime.now(timezone.utc)


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_s(dt):
    if dt is None:
        return None
    return int((_now() - _utc(dt)).total_seconds())


def _coverage(competition_slug: str | None = None) -> dict:
    """Paper-ledger completeness, flattened for the metrics surface.
    Isolated so a coverage failure can never take the whole metrics
    endpoint down with it."""
    try:
        from src.live import paper
        cov = paper.paper_coverage(competition_slug=competition_slug)
        return {k: cov.get(k) for k in
                ("locks_eligible", "locks_covered", "locks_uncovered",
                 "legs_eligible", "legs_signalled", "legs_missing",
                 "coverage_pct", "complete", "backfilled_signals")}
    except Exception as exc:
        return {"error": str(exc)[:120]}


def metrics(competition_slug: str | None = None) -> dict:
    """Operational metrics for ONE competition.

    Every count here used to be a whole-table count (mapped events, runs,
    snapshots, paper signals and fills), so the moment a second
    competition existed the MLS metrics surface absorbed its state — a
    dark EPL plane could inflate MLS's mapped-event count and, worse, an
    EPL failed snapshot would read as an MLS lock failure (EPL P0-2)."""
    if not plane_ready():
        return {"skipped": "dormant"}
    slug = competition_slug or competitions.DEFAULT_SLUG
    competitions.spec(slug)                # unknown slug -> raise
    s = get_session()
    try:
        # fixture freshness — how long since the newest observation
        newest_fx = (s.query(Fixture)
                     .filter_by(competition_slug=slug)
                     .order_by(Fixture.observed_at.desc()).first())
        comp_fixtures = s.query(Fixture).filter_by(
            competition_slug=slug).all()
        fixture_ids = {f.id for f in comp_fixtures}
        comp_runs = [r for r in s.query(PredictionRun).all()
                     if r.fixture_id in fixture_ids]
        # locks: kicked-off shadow-touched fixtures should each have one
        touched = {r.fixture_id for r in comp_runs}
        kicked = [f for f in comp_fixtures
                  if f.id in touched and f.current_kickoff_utc
                  and _utc(f.current_kickoff_utc) < _now()]
        locked = [f for f in kicked
                  if s.query(PredictionRun)
                  .filter_by(fixture_id=f.id, run_type="t10",
                             canonical=True, status="complete").first()]
        comp_snaps = [sn for sn in s.query(MarketSnapshot).all()
                      if sn.fixture_id in fixture_ids]
        # snapshot freshness (the freshest lock book's quote age)
        complete_snaps = [sn for sn in comp_snaps if sn.status == "complete"
                          and sn.captured_at is not None]
        latest_snap = (max(complete_snaps, key=lambda sn: _utc(sn.captured_at))
                       if complete_snaps else None)
        # paper economics
        comp_run_ids = {r.id for r in comp_runs}
        comp_signals = [g for g in s.query(PaperSignal).all()
                        if g.prediction_run_id in comp_run_ids]
        sig_ids = {g.id for g in comp_signals}
        fills = [f for f in s.query(PaperFill).all()
                 if f.paper_signal_id in sig_ids]
        settled = [f for f in fills if f.status == "settled"]
        pnl = sum(f.pnl_c or 0 for f in settled)
        # settlement lag: open fills whose fixture is already post
        stale_settle = 0
        for fill, sig, fx in (s.query(PaperFill, PaperSignal, Fixture)
                              .join(PaperSignal,
                                    PaperFill.paper_signal_id
                                    == PaperSignal.id)
                              .join(Fixture,
                                    PaperSignal.fixture_id == Fixture.id)
                              .filter(PaperFill.status == "open",
                                      Fixture.status == "post",
                                      Fixture.competition_slug == slug)
                              .all()):
            stale_settle += 1
        return {
            "generated_at": _now().isoformat(),
            "competition_slug": slug,
            "data": {
                "fixture_obs_age_s": _age_s(
                    newest_fx.observed_at if newest_fx else None),
                "latest_lock_snapshot_age_s": _age_s(
                    latest_snap.captured_at if latest_snap else None),
                "latest_snapshot_quote_age_s": (
                    latest_snap.oldest_quote_age_seconds
                    if latest_snap else None),
                "mapped_market_events": s.query(MarketEvent)
                .filter_by(competition_slug=slug,
                           mapping_approved=True).count(),
            },
            "locks": {
                "kicked_off_shadow_fixtures": len(kicked),
                "canonical_locked": len(locked),
                "missed_locks": len(kicked) - len(locked),
                "lock_success_rate": (round(len(locked) / len(kicked), 3)
                                      if kicked else None),
                "failed_snapshots": sum(1 for sn in comp_snaps
                                        if sn.status == "failed"),
            },
            "runs": {
                "complete": sum(1 for r in comp_runs
                                if r.status == "complete"),
                "failed": sum(1 for r in comp_runs if r.status == "failed"),
            },
            "paper": {
                "signals": len(comp_signals),
                # a signal count WITHOUT this is unreadable: the 2026-07-25
                # slate reported 27 against 45 eligible legs and nothing
                # anywhere said so
                "coverage": _coverage(slug),
                "fills": len(fills),
                "open": sum(1 for f in fills if f.status == "open"),
                "settled": len(settled),
                "settled_pnl_c": pnl,
                "unsettled_after_final": stale_settle,
            },
        }
    finally:
        s.close()


# --- volume headroom ------------------------------------------------------
# Railway's volume alerts are Teams/Pro-only, so nothing on the platform
# will warn before the disk fills. It filled on 2026-07-25 and every
# prediction write failed silently behind {"created": 0} — the failure
# mode this watches for.

_last_storage_alert: datetime | None = None


def storage_headroom() -> dict:
    """Live-plane volume usage against configured capacity.

    Postgres reports its own database size; it cannot see the size of the
    volume underneath it, so capacity comes from config
    (LIVE_VOLUME_BYTES) and must be kept in step with Railway if the
    volume is resized. `capacity_is_configured` says so plainly rather
    than letting a stale constant read as a measurement.
    """
    import config
    from sqlalchemy import text

    from src.live.db import get_engine
    eng = get_engine()
    if eng is None:
        return {"dormant": True}
    with eng.connect() as c:
        used = int(c.execute(text(
            "SELECT pg_database_size(current_database())")).scalar() or 0)
    cap = int(config.LIVE_VOLUME_BYTES)
    pct = round(100.0 * used / cap, 2) if cap > 0 else None
    return {
        "database_bytes": used,
        "volume_bytes": cap,
        "used_pct": pct,
        "free_bytes": max(cap - used, 0),
        "threshold_pct": config.STORAGE_ALERT_PCT,
        "over_threshold": bool(pct is not None
                               and pct >= config.STORAGE_ALERT_PCT),
        "capacity_is_configured": True,
        "capacity_note": ("volume size is config, not a measurement — "
                          "Railway cannot expose it to the container"),
    }


def check_storage_headroom(force: bool = False) -> dict:
    """Alert when the live volume crosses its threshold. Cooldown-limited
    so a slowly filling disk does not page hourly. Returns what it saw so
    the caller can log it even when no alert fires."""
    global _last_storage_alert
    import config
    rep = storage_headroom()
    if rep.get("dormant") or not rep.get("over_threshold"):
        return {**rep, "alerted": False}
    now = _now()
    if not force and _last_storage_alert is not None:
        mins = (now - _last_storage_alert).total_seconds() / 60.0
        if mins < config.STORAGE_ALERT_COOLDOWN_MINUTES:
            return {**rep, "alerted": False, "suppressed": "cooldown"}
    gb = 1024.0 ** 3
    msg = (f"⚠️ LIVE VOLUME {rep['used_pct']}% "
           f"({rep['database_bytes'] / gb:.2f} GB of "
           f"{rep['volume_bytes'] / gb:.2f} GB) — threshold "
           f"{rep['threshold_pct']}%. A full volume fails prediction "
           f"writes SILENTLY (2026-07-25). Resize the Railway volume or "
           f"prune market_depth_level / source_observation.")
    try:
        # OPERATIONAL: headroom on the platform's own volume. Governed by
        # the cooldown above, never by the money lock — a full volume
        # fails prediction writes silently, and the silence is the whole
        # danger this alert exists to break.
        from src.alerts import OPERATIONAL, send_alert
        send_alert(msg, title="Trivela storage",
                   dispatch_class=OPERATIONAL)
        _last_storage_alert = now
        return {**rep, "alerted": True}
    except Exception as exc:      # alerting must never break the caller
        print(f"[storage] alert failed: {exc}")
        return {**rep, "alerted": False, "error": str(exc)}
