"""Released XIs for viewer competitions, from ESPN.

WHY THIS EXISTS. `team_news.capture_league_lineup` asks API-Football,
whose lineups endpoint returns NOTHING until a match is under way
(measured 2026-08-05: 0 results at T-45 on three fixtures, its own
docstring says the same). So the XI — the largest single information
event before kickoff — was uncapturable on every viewer competition.
ESPN publishes full XIs with formations around kickoff (verified the
same night: 11 starters and a formation on both sides of two live
fixtures), and `capture_friendly_lineup` already reads exactly that
shape — but only for friendlies, which carry ESPN ids natively.

THE BRIDGE IS THE HARD PART, and it is the one this repo has learned
the most about. Viewer fixtures carry API-Football ids; ESPN events
carry ESPN ids. Joining them is a name-plus-date join, which the
friendlies surface bans outright for its own identity reasons — but
here there is no alternative identifier, so the join is made under the
three guards the sharp-anchor bridge earned the hard way (#74):

  BOTH CLUBS must match on identity tokens, never one;
  a KICKOFF WINDOW (6h) must contain both, so the same pairing's other
    leg or a later meeting cannot attach;
  REVERSE UNIQUENESS — one ESPN event backs at most one fixture, and
    two claimants refuse both rather than one being guessed.

Orientation-agnostic, like every other bridge here: ESPN lists
"Querétaro at FC Dallas" while the fixture feed says Dallas is home,
and the SIDES are resolved by name afterwards, never by position.

NOT A MODEL INPUT. Same boundary as the rest of team news: this writes
display tables only. Lineup features were measured negative-or-marginal
and are off; nothing here reaches a lock, a run, or a paper signal.
"""
from __future__ import annotations

import threading
import time

# PROBED 2026-08-05 against ESPN's own scoreboard (HTTP 200 + the
# league name it reports), never guessed from the competition name —
# the KXSERIEAGAME rule applied to a second provider.
ESPN_SLUGS = {
    "leagues-cup": "concacaf.leagues.cup",   # "Leagues Cup"
    "ucl": "uefa.champions",                 # "UEFA Champions League"
    "uel": "uefa.europa",                    # "UEFA Europa League"
    "ecl": "uefa.europa.conf",               # "UEFA Conference League"
    "brasileirao": "bra.1",                  # "Brazilian Serie A"
    "argentina": "arg.1",                    # "Argentine Liga Profesional"
    "usl": "usa.usl.1",                      # "USL Championship"
    "asean": "aff.championship",             # "ASEAN Championship"
}

# Providers that abbreviate a club to an initialism share NO token with
# a provider that spells it out: ESPN's "LAFC" against our "Los Angeles
# FC" is zero overlap, and both-clubs-must-match correctly refuses.
# Weakening the rule to rescue it would readmit every false attach the
# rule exists to stop, so the fix is a PINNED alias — each entry
# verified against both providers by hand, on the same fixture.
#
# Verified 2026-08-05 on Leagues Cup matchday 2 (ESPN event 401863560,
# "LAFC v Guadalajara" = our "Los Angeles FC v Guadalajara Chivas").
NAME_ALIASES = {
    "lafc": {"los", "angeles"},
}

WINDOW_SECONDS = 6 * 3600
SCOREBOARD_TTL = 300.0
_lock = threading.Lock()
_board: dict[str, tuple[float, list]] = {}


def _events(slug: str, day: str | None) -> list[dict] | None:
    """ESPN's scoreboard for one competition-day. None on FAILURE — never
    cached, never read as "no matches"."""
    key = f"{slug}:{day or 'today'}"
    now = time.monotonic()
    with _lock:
        hit = _board.get(key)
        if hit and now - hit[0] < SCOREBOARD_TTL:
            return hit[1]
    import requests
    from src.live.team_news import ESPN_SOCCER_BASE
    try:
        r = requests.get(f"{ESPN_SOCCER_BASE}/{slug}/scoreboard",
                         params={"dates": day} if day else None, timeout=15)
        if r.status_code != 200:
            return None
        evs = r.json().get("events") or []
    except Exception:
        return None
    with _lock:
        _board[key] = (now, evs)
    return evs


def _when(s):
    from datetime import datetime, timezone
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _sides(ev: dict) -> tuple[str, str]:
    """(home, away) names from an ESPN event's competitors."""
    comp = (ev.get("competitions") or [{}])[0]
    h = a = ""
    for c in comp.get("competitors") or []:
        name = ((c.get("team") or {}).get("displayName")
                or (c.get("team") or {}).get("name") or "")
        if c.get("homeAway") == "home":
            h = name
        elif c.get("homeAway") == "away":
            a = name
    return h, a


def bridge(rows: list[dict], comp_key: str) -> tuple[dict, list[dict]]:
    """({fixture_id: espn_event_id}, unmatched-with-reasons).

    Rows are viewer fixtures; the ESPN board is fetched per kickoff day
    (ESPN buckets by US-Eastern date, so the day before and after are
    swept too — the same +-1 sweep live_feed uses)."""
    slug = ESPN_SLUGS.get(comp_key)
    if not slug:
        return {}, [{"reason": f"no probed ESPN slug for {comp_key}"}]
    from src import friendlies as _fr

    seen: dict[str, dict] = {}
    for r in rows:
        ko = _when(r.get("kickoff_utc"))
        if ko is None:
            continue
        for delta in (-1, 0, 1):
            from datetime import timedelta
            day = (ko + timedelta(days=delta)).strftime("%Y%m%d")
            for e in (_events(slug, day) or []):
                seen[str(e.get("id"))] = e

    def _toks(name: str) -> frozenset:
        t = set(_fr._identity_tokens(name))
        for k, extra in NAME_ALIASES.items():
            if k in t:
                t |= extra
        return frozenset(t)

    tagged = []
    for e in seen.values():
        hn, an = _sides(e)
        tagged.append((e, _toks(hn), _toks(an), _when((e.get("date") or ""))))

    provisional: list[tuple[dict, dict]] = []
    unmatched: list[dict] = []
    for r in rows:
        hn = (r.get("home") or {}).get("name") or ""
        an = (r.get("away") or {}).get("name") or ""
        ht, at = _toks(hn), _toks(an)
        ko = _when(r.get("kickoff_utc"))
        who = {"fixture_id": r.get("fixture_id"), "home": hn, "away": an}
        if not ht or not at or ko is None:
            unmatched.append({**who, "reason": "no tokens or no kickoff"})
            continue
        hits = []
        for e, eh, ea, ed in tagged:
            both = ((ht & eh) and (at & ea)) or ((ht & ea) and (at & eh))
            if not both:
                continue
            if ed is None or abs((ed - ko).total_seconds()) > WINDOW_SECONDS:
                continue
            hits.append(e)
        if len(hits) != 1:
            unmatched.append({**who, "reason": (
                "no ESPN event matches both clubs inside the window"
                if not hits else
                f"{len(hits)} ESPN events match both clubs; none attached")})
            continue
        provisional.append((r, hits[0]))

    claims: dict[str, int] = {}
    for _r, e in provisional:
        claims[str(e.get("id"))] = claims.get(str(e.get("id")), 0) + 1
    out: dict[int, str] = {}
    for r, e in provisional:
        eid = str(e.get("id"))
        if claims[eid] > 1:
            unmatched.append({"fixture_id": r.get("fixture_id"),
                              "home": (r.get("home") or {}).get("name"),
                              "away": (r.get("away") or {}).get("name"),
                              "reason": ("one ESPN event is the sole match "
                                         "for more than one fixture; all "
                                         "refused")})
            continue
        out[r["fixture_id"]] = eid
    return out, unmatched


def capture(fixture_id: int, espn_event_id: str, comp_key: str,
            kickoff_utc=None, summary: dict | None = None) -> dict:
    """Capture one viewer fixture's XI from ESPN into the display tables.

    Stored under the ESPN provider against the API-FOOTBALL fixture ref,
    so the viewer surface (which keys on that ref) finds it beside the
    API-Football rows rather than in a parallel world.
    """
    from src.live import team_news as tn
    if not tn.plane_ready():
        return {"state": "dormant", "feed": "lineup"}
    slug = ESPN_SLUGS.get(comp_key)
    if not slug:
        return {"state": "unavailable", "feed": "lineup",
                "note": f"no probed ESPN slug for {comp_key}"}
    # _store_lineup_states feeds kickoff_utc to _utc(), which expects a
    # DATETIME; a viewer row carries an ISO string, and passing it
    # through raised on the first live fixture ('str' has no tzinfo).
    ko = _when(kickoff_utc) if isinstance(kickoff_utc, str) else kickoff_utc
    f = (tn.Fetched("ok", [summary]) if summary is not None
         else tn._espn_summary(str(espn_event_id), slug))
    s = tn.get_session()
    try:
        body = f.items[0] if f.items else {}
        states = (tn.espn_lineup_states(body) if f.state == "ok" else {})
        sides = tn._store_lineup_states(s, tn.PROVIDER_ESPN,
                                        str(fixture_id), states,
                                        None, comp_key, ko)
        tn._record_capture(s, tn.PROVIDER_ESPN, str(fixture_id), "lineup",
                           f, None, comp_key)
        s.commit()
        return {"state": f.state, "feed": "lineup",
                "provider": tn.PROVIDER_ESPN, "sides": sides,
                "espn_event_id": str(espn_event_id)}
    except Exception as exc:
        s.rollback()
        print(f"[espn-lineups] {fixture_id}: {tn._redact(exc)}")
        return {"state": "unavailable", "feed": "lineup",
                "note": str(exc)[:120]}
    finally:
        s.close()
