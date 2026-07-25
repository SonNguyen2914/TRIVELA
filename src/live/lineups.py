"""Lineup / availability capture (V8.1 evaluation Phase 5).

ESPN's match summary carries `rosters`: per team a formation and a
roster where each entry has `starter`, `position`, `athlete`, `jersey`.
A lineup is CONFIRMED when a team has 11 starters (ESPN populates this
~1h before kickoff); before that it is PENDING. The goalkeeper is the
starter whose position abbreviation is 'G'.

We snapshot that state with full provenance (observed_at, provider,
parser_version, content-hashed source observation) so a T-10 lock can
reference exactly what team-selection information existed when it was
made. The model does NOT yet consume this — per the decision, lineup
effects wait for validation — but the shadow record captures whether
the data was there, so missing data is never silently treated as
confidence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import requests

import config
from src.live.evidence import pack_payload as _pack
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, LineupEntry, LineupSnapshot, Player,
                             SourceObservation)

PARSER_VERSION = "espn-lineup-v1"
SUMMARY_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
               "usa.1/summary")


def _now():
    return datetime.now(timezone.utc)


def parse_lineup(summary: dict) -> dict:
    """Pure: ESPN summary -> per-side selection state. Canned-testable."""
    out = {"home": None, "away": None}
    for team in summary.get("rosters") or []:
        side = team.get("homeAway")
        if side not in ("home", "away"):
            continue
        roster = team.get("roster") or []
        entries = []
        for e in roster:
            ath = e.get("athlete") or {}
            pos = (e.get("position") or {}).get("abbreviation")
            entries.append({
                "espn_id": str(ath.get("id")) if ath.get("id") else None,
                "name": ath.get("displayName"),
                "starter": bool(e.get("starter")),
                "is_goalkeeper": pos == "G" and bool(e.get("starter")),
                "position": pos,
                "jersey": e.get("jersey"),
            })
        starters = [x for x in entries if x["starter"]]
        gk = next((x for x in starters if x["is_goalkeeper"]), None)
        out[side] = {
            "formation": team.get("formation"),
            "confirmed": len(starters) == 11,
            "starters": len(starters),
            "goalkeeper": gk,
            "entries": entries,
        }
    return out


def _status(parsed: dict) -> str:
    h, a = parsed.get("home"), parsed.get("away")
    hc = bool(h and h["confirmed"])
    ac = bool(a and a["confirmed"])
    if hc and ac:
        return "confirmed"
    if hc or ac:
        return "partial"
    return "pending"


def _upsert_player(s, competition, e) -> int | None:
    if not e.get("espn_id"):
        return None
    p = s.query(Player).filter_by(espn_id=e["espn_id"]).first()
    if p is None:
        p = Player(competition_slug=competition, espn_id=e["espn_id"],
                   name=e.get("name"), position=e.get("position"))
        s.add(p)
        s.flush()
    return p.id


def _empty_quality() -> dict:
    """Input-quality states for a non-observation — every flag False, so
    a missing lineup is recorded as missing, never absorbed as truth."""
    return {"LINEUP_CONFIRMED": False, "GOALKEEPER_CONFIRMED": False,
            "AVAILABILITY_COMPLETE": False, "PLAYER_DATA_FRESH": False}


def _record_unavailable(s, fixture_id: int, status: str,
                        note: str) -> dict:
    """Persist a snapshot for a NON-observation — a fetch failure or an
    unavailable lineup (V9 eval F2). The model doesn't consume lineups,
    so this does not block the market/model lock, but the lock must still
    reference EXPLICIT provenance: a null lineup id would let a canonical
    lock pass with no lineup reference and fail its own audit. Missing
    data is recorded, never a silent None."""
    snap = LineupSnapshot(
        fixture_id=fixture_id, captured_at=_now(), observed_at=None,
        provider="espn", parser_version=PARSER_VERSION,
        source_observation_id=None, status=status,
        home_confirmed=False, away_confirmed=False)
    s.add(snap)
    s.flush()
    s.commit()
    return {"snapshot_id": snap.id, "status": status,
            "quality": _empty_quality(), "note": note[:200]}


def capture_lineup(fixture_id: int, summary: dict | None = None
                   ) -> dict | None:
    """Fetch (or accept a canned) ESPN summary, parse the selection
    state, and write a provenance-complete LineupSnapshot + entries.
    Returns {'snapshot_id', 'status', 'quality'}; None only when the plane
    is dormant or the fixture is unknown. ALWAYS records a snapshot when
    the fixture exists — a pending lineup, an unavailable one, and a fetch
    FAILURE are all evidence, and each gets an explicit snapshot (V9 eval
    F2) so a T-10 lock never references a null lineup."""
    if not plane_ready():
        return None
    s = get_session()
    try:
        fx = s.get(Fixture, fixture_id)
        if fx is None:
            return None
        if summary is None:
            try:
                r = requests.get(SUMMARY_URL,
                                 params={"event": fx.espn_event_id},
                                 timeout=15)
                r.raise_for_status()
                summary = r.json()
            except requests.RequestException as exc:
                print(f"[lineups] fetch {fx.espn_event_id}: {exc}")
                return _record_unavailable(
                    s, fixture_id, "fetch_failed", str(exc))
        parsed = parse_lineup(summary)
        raw = json.dumps(summary.get("rosters") or [], sort_keys=True)
        obs = SourceObservation(
            source="espn",
            endpoint=f"summary?event={fx.espn_event_id}#rosters",
            observed_at=_now(), **_pack(raw))
        s.add(obs)
        s.flush()
        snap = LineupSnapshot(
            fixture_id=fixture_id, captured_at=_now(), observed_at=_now(),
            provider="espn", parser_version=PARSER_VERSION,
            source_observation_id=obs.id, status=_status(parsed),
            home_confirmed=bool(parsed["home"] and parsed["home"]["confirmed"]),
            away_confirmed=bool(parsed["away"] and parsed["away"]["confirmed"]),
            home_formation=(parsed["home"] or {}).get("formation"),
            away_formation=(parsed["away"] or {}).get("formation"))
        s.add(snap)
        s.flush()
        for side in ("home", "away"):
            side_data = parsed.get(side)
            if not side_data:
                continue
            gk = side_data.get("goalkeeper")
            if gk:
                gk_id = _upsert_player(s, fx.competition_slug, gk)
                setattr(snap, f"{side}_gk_player_id", gk_id)
            for e in side_data["entries"]:
                pid = _upsert_player(s, fx.competition_slug, e)
                s.add(LineupEntry(
                    lineup_snapshot_id=snap.id, side=side, player_id=pid,
                    starter=e["starter"], is_goalkeeper=e["is_goalkeeper"],
                    position=e["position"], jersey=e["jersey"]))
        quality = lineup_quality(parsed)
        s.commit()
        return {"snapshot_id": snap.id, "status": snap.status,
                "quality": quality}
    except Exception as exc:
        s.rollback()
        print(f"[lineups] capture failed: {exc}")
        return None
    finally:
        s.close()


def lineup_quality(parsed: dict) -> dict:
    """The lineup-derived input-quality states."""
    h, a = parsed.get("home"), parsed.get("away")
    lineup_confirmed = bool(h and h["confirmed"] and a and a["confirmed"])
    gk_confirmed = bool(h and h.get("goalkeeper")
                        and a and a.get("goalkeeper"))
    return {
        "LINEUP_CONFIRMED": lineup_confirmed,
        "GOALKEEPER_CONFIRMED": gk_confirmed,
        # v1: no injury/suspension feed yet — availability completeness
        # is proxied by a confirmed lineup, and honestly labeled so
        "AVAILABILITY_COMPLETE": lineup_confirmed,
        "PLAYER_DATA_FRESH": bool(h or a),
    }
