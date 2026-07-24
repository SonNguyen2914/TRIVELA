"""Match-page lineup view: the announced XI, annotated with each player's
attacking strength, plus the notable names NOT starting.

This is DISPLAY CONTEXT, not a model input. The walk-forward tests were
explicit (research_archive/mls_targeted_lineup_features_2026-07-24.json):
an XI-strength adjustment does NOT beat team-xG, and the one real signal
(key-attacker availability, +0.0034) is not significant — so none of this
moves a probability. It answers the reader's question — "who's actually
playing, and is anyone important missing?" — and nothing more.

Composition:
  - the XI + formation come from ESPN (lineups.parse_lineup);
  - a player's strength (xG per 90) comes from the official MLS/Sportec
    per-match player stats;
  - the two are joined by player.sportec_id, the validated ESPN<->Sportec
    bridge (99.5% on starters).

Degrades gracefully at every step: no live plane, no bridge, or no
lineup released yet each yield a smaller payload, never an error.
"""
from __future__ import annotations

from collections import defaultdict

from src.live import identity, lineups
from src.live.db import get_session, plane_ready
from src.live.models import MlsPlayerMatchStat, Player

# a "regular" needs this many appearances before we call them a key player
MIN_APPS = 3
TOP_N = 3          # how many of a team's top attackers we track
MIN_XG90 = 0.15    # below this, an absence isn't newsworthy


def _strength_by_sportec(s) -> dict:
    """sportec_id -> {name, xg90, apps, minutes} over the season."""
    agg = defaultdict(lambda: {"xg": 0.0, "mins": 0.0, "apps": 0,
                               "name": None, "team_id": None})
    for p in s.query(MlsPlayerMatchStat).all():
        if not p.sportec_player_id:
            continue
        a = agg[p.sportec_player_id]
        a["xg"] += float(p.xg or 0.0)
        a["mins"] += float(p.minutes or 0.0)
        a["name"] = a["name"] or p.player_name
        a["team_id"] = p.team_id or a["team_id"]
        if (p.minutes or 0) >= 60:
            a["apps"] += 1
    out = {}
    for pid, a in agg.items():
        if a["mins"] <= 0:
            continue
        out[pid] = {"name": a["name"], "apps": a["apps"],
                    "minutes": round(a["mins"]),
                    "team_id": a["team_id"],
                    "xg90": round(a["xg"] / a["mins"] * 90, 3)}
    return out


def _espn_to_sportec(s) -> dict:
    return {p.espn_id: p.sportec_id for p in s.query(Player)
            if p.espn_id and p.sportec_id}


def build(summary: dict) -> dict:
    """ESPN summary -> the match page's lineup section."""
    parsed = lineups.parse_lineup(summary)
    view = {"home": None, "away": None, "strength_available": False}
    if not any(parsed.get(k) for k in ("home", "away")):
        return view

    # side -> team abbreviation, so we can find each club's regulars
    abbrev = {}
    for c in ((summary.get("header") or {}).get("competitions")
              or [{}])[0].get("competitors") or []:
        t = c.get("team") or {}
        if c.get("homeAway") in ("home", "away"):
            abbrev[c["homeAway"]] = t.get("abbreviation")

    strength, bridge = {}, {}
    if plane_ready():
        s = get_session()
        try:
            strength = _strength_by_sportec(s)
            bridge = _espn_to_sportec(s)
        finally:
            s.close()
    view["strength_available"] = bool(strength and bridge)

    for side in ("home", "away"):
        side_data = parsed.get(side)
        if not side_data:
            continue
        entries = side_data.get("entries") or []
        # annotate every player we can identify
        announced = set()
        for e in entries:
            sid = bridge.get(e.get("espn_id"))
            st = strength.get(sid) if sid else None
            e["xg90"] = st["xg90"] if st else None
            e["apps"] = st["apps"] if st else None
            if sid:
                announced.add(sid)
        starters = [e for e in entries if e.get("starter")]
        bench = [e for e in entries if not e.get("starter")]
        starting_ids = {bridge.get(e.get("espn_id")) for e in starters}
        starting_ids.discard(None)

        # A lineup that hasn't been RELEASED yet must never be read as
        # "everyone is missing": with no XI announced, every key player
        # would otherwise render as an absence. Absences are only
        # computed once a full XI exists.
        released = len(starters) >= 11
        absences = []
        team = identity.resolve_mls_club(abbrev.get(side) or "")
        if released and team is not None and strength:
            regulars = sorted(
                ((v["xg90"], pid, v) for pid, v in strength.items()
                 if v["team_id"] == team.id and v["apps"] >= MIN_APPS
                 and v["xg90"] >= MIN_XG90),
                reverse=True)[:TOP_N]
            for xg90, pid, v in regulars:
                if pid in starting_ids:
                    continue
                # on the bench (named but not starting) vs out of the squad
                on_bench = pid in announced
                absences.append({
                    "name": v["name"], "xg90": xg90, "apps": v["apps"],
                    "status": "bench" if on_bench else "out",
                })

        view[side] = {
            "formation": side_data.get("formation"),
            "confirmed": side_data.get("confirmed"),
            # explicit: has a starting XI been announced at all? the page
            # shows "awaiting team news" rather than an empty/absent XI
            "released": released,
            "starters": [
                {k: e.get(k) for k in
                 ("name", "position", "jersey", "xg90", "apps",
                  "is_goalkeeper")}
                for e in starters],
            "bench": [{k: e.get(k) for k in ("name", "position", "jersey",
                                             "xg90")} for e in bench],
            "goalkeeper": (side_data.get("goalkeeper") or {}).get("name"),
            "key_absences": absences,
        }
    return view
