"""ESPN <-> Sportec player identity bridge.

The released lineup for an upcoming match is ESPN-identified (athlete ids);
a player's strength history is Sportec-identified (MLS-OBJ ids). To price a
lineup against player strength we need a durable id map.

It is built by matching PER-MATCH PARTICIPANT LISTS: for each completed
fixture, ESPN's match summary and our Sportec stats list the same ~20
players per side, so a name match runs against a tiny, high-precision
candidate set — validated 99.5% on starters (vs ~83% against the stale,
transfer-churned team-roster endpoint). The map accumulates every player
who has appeared, so an upcoming lineup's players are looked up directly.

The bridge is stored as player.sportec_id. Additive identity only — it
changes no model output on its own.
"""
from __future__ import annotations

import time
import unicodedata
from collections import defaultdict

import requests

from src.live import lineups
from src.live.db import get_session, plane_ready
from src.live.models import Fixture, MlsPlayerMatchStat, Player

THROTTLE_SECONDS = 0.3


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace(".", "").replace("-", " ").split())


def _toks(s: str) -> set:
    return set(_norm(s).split())


def match_name(sp_name: str, espn: list[tuple]) -> str | None:
    """Match a Sportec player name to an ESPN athlete id within a match's
    participant set. espn = [(espn_id, full_norm, token_set)]. Ordered by
    precision: exact -> token-set equal -> subset/mononym -> unique last
    name. Returns the espn_id or None."""
    spn = _norm(sp_name)
    if not spn:
        return None
    spt = set(spn.split())
    for eid, fn, ts in espn:
        if fn == spn:
            return eid
    for eid, fn, ts in espn:
        if ts == spt:
            return eid
    for eid, fn, ts in espn:
        sh = spt & ts
        if (spt <= ts or ts <= spt) and (
                len(sh) >= 2 or (min(len(spt), len(ts)) == 1 and sh)):
            return eid
    sl = spn.split()[-1]
    cands = [eid for eid, fn, ts in espn if fn.split()[-1] == sl]
    if len(cands) == 1:
        return cands[0]
    return None


def _summary_participants(espn_event_id: str) -> list[tuple]:
    """ESPN match-summary participants: [(espn_id, name)] over both sides."""
    try:
        d = requests.get(lineups.SUMMARY_URL,
                         params={"event": espn_event_id}, timeout=15).json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for side in d.get("rosters") or []:
        for e in side.get("roster") or []:
            a = e.get("athlete") or {}
            nm = a.get("fullName") or a.get("displayName")
            if a.get("id") and nm:
                out.append((str(a["id"]), nm))
    return out


def _sportec_players_by_fixture(s) -> dict:
    out = defaultdict(list)
    for p in s.query(MlsPlayerMatchStat).all():
        if p.sportec_player_id and p.player_name:
            out[p.fixture_id].append((p.sportec_player_id, p.player_name))
    return out


def _upsert_map(s, sportec_id, espn_id, name, comp="mls-2026") -> bool:
    """Set player.sportec_id, enforcing one-to-one. Returns created/updated?
    A sportec_id already held by a different espn player is a conflict —
    left alone and reported, never silently reassigned."""
    taken = (s.query(Player)
             .filter(Player.sportec_id == sportec_id).first())
    if taken is not None and taken.espn_id != espn_id:
        return False
    row = s.query(Player).filter_by(espn_id=espn_id).first()
    if row is None:
        row = Player(competition_slug=comp, espn_id=espn_id, name=name[:96])
        s.add(row)
    if row.sportec_id == sportec_id:
        return False
    row.sportec_id = sportec_id
    if not row.name:
        row.name = name[:96]
    return True


def build_bridge(max_fixtures: int | None = None,
                 skip_covered: bool = True) -> dict:
    """Populate player.sportec_id by matching ESPN summary participants to
    Sportec players across completed fixtures. skip_covered avoids the ESPN
    fetch for a fixture whose Sportec players are already all mapped, so
    re-runs are cheap and the first pass covers everyone."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    mapped = fixtures_fetched = conflicts = 0
    try:
        spo = _sportec_players_by_fixture(s)
        already = {r.sportec_id for r in s.query(Player)
                   .filter(Player.sportec_id.isnot(None))}
        rows = (s.query(Fixture)
                .filter_by(competition_slug="mls-2026", status="post")
                .filter(Fixture.espn_event_id.isnot(None)).all())
        rows = [f for f in rows if f.id in spo]
        if max_fixtures:
            rows = rows[:max_fixtures]
        for f in rows:
            players = spo[f.id]
            if skip_covered and all(pid in already for pid, _ in players):
                continue
            parts = _summary_participants(f.espn_event_id)
            if not parts:
                continue
            fixtures_fetched += 1
            espn = [(eid, _norm(nm), _toks(nm)) for eid, nm in parts]
            for sportec_id, sp_name in players:
                if sportec_id in already:
                    continue
                eid = match_name(sp_name, espn)
                if eid is None:
                    continue
                if _upsert_map(s, sportec_id, eid, sp_name):
                    mapped += 1
                    already.add(sportec_id)
                else:
                    conflicts += 1
            s.commit()
            time.sleep(THROTTLE_SECONDS)
        return {"newly_mapped": mapped, "fixtures_fetched": fixtures_fetched,
                "conflicts": conflicts}
    except Exception as exc:
        s.rollback()
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def bridge_coverage() -> dict:
    """How many Sportec players (and how much of the minutes actually
    played) carry an ESPN id — the verifiable bridge health."""
    if not plane_ready():
        return {"dormant": True}
    s = get_session()
    try:
        # distinct sportec players + their total minutes
        mins = defaultdict(float)
        for p in s.query(MlsPlayerMatchStat).all():
            if p.sportec_player_id:
                mins[p.sportec_player_id] += float(p.minutes or 0.0)
        mapped = {r.sportec_id for r in s.query(Player)
                  .filter(Player.sportec_id.isnot(None))}
        total = len(mins)
        cov = sum(1 for pid in mins if pid in mapped)
        wm = sum(m for pid, m in mins.items() if pid in mapped)
        wt = sum(mins.values()) or 1.0
        return {
            "sportec_players": total,
            "mapped": cov,
            "coverage": round(cov / total, 3) if total else 0.0,
            "minutes_weighted_coverage": round(wm / wt, 3),
        }
    finally:
        s.close()


def sportec_for_espn(espn_id: str) -> str | None:
    s = get_session()
    if s is None:
        return None
    try:
        r = s.query(Player).filter_by(espn_id=str(espn_id)).first()
        return r.sportec_id if r else None
    finally:
        s.close()
