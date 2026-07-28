"""Identity seeding + resolution for MLS (launch decision O4).

The 30 clubs are seeded from ESPN's teams endpoint (canonical names +
ESPN ids), and the Kalshi name bridges are seeded as APPROVED aliases —
approval here is the operator-reviewed curated map below, not fuzzy
matching. Resolution helpers only ever consult approved rows, per the
decision: fuzzy may propose, the alias table decides.

API-Football ids arrive later via discovery calls (the decision:
discover through the API, don't copy from memory).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

import requests

from src.live.db import get_session, plane_ready
from src.live.models import Team, TeamAlias

ESPN_TEAMS = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
              "usa.1/teams")

# Operator-curated Kalshi-name bridges (verified against the live
# KXMLSGAME slates of Jul 22/25). Keys = Kalshi title sides, values =
# the ESPN displayName they attach to. Seeded as APPROVED aliases.
KALSHI_BRIDGES = {
    "Los Angeles G": "LA Galaxy",
    "Los Angeles F": "LAFC",
    "Saint Louis": "St. Louis CITY SC",
    "New York RB": "Red Bull New York",   # ESPN's word order, not "NY Red Bulls"
    "New York City": "New York City FC",
    "Chicago Fire": "Chicago Fire FC",
    "Miami": "Inter Miami CF",
    "Montreal": "CF Montréal",
    "Salt Lake": "Real Salt Lake",
    "Kansas City": "Sporting Kansas City",
    "DC United": "D.C. United",
    "Atlanta": "Atlanta United FC",
    "Austin": "Austin FC",
    "Charlotte": "Charlotte FC",
    "Cincinnati": "FC Cincinnati",
    "Columbus": "Columbus Crew",
    "Colorado": "Colorado Rapids",
    "Dallas": "FC Dallas",
    "Houston": "Houston Dynamo FC",
    "Minnesota": "Minnesota United FC",
    "Nashville": "Nashville SC",
    "New England": "New England Revolution",
    "Orlando": "Orlando City SC",
    "Philadelphia": "Philadelphia Union",
    "Portland": "Portland Timbers",
    "San Diego FC": "San Diego FC",
    "San Jose": "San Jose Earthquakes",
    "Seattle": "Seattle Sounders FC",
    "Toronto": "Toronto FC",
    "Vancouver": "Vancouver Whitecaps",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").strip()


def fetch_espn_teams(teams_url: str = ESPN_TEAMS) -> list[dict]:
    r = requests.get(teams_url, timeout=15)
    r.raise_for_status()
    leagues = r.json().get("sports", [{}])[0].get("leagues", [{}])[0]
    return [t.get("team", {}) for t in leagues.get("teams", [])]


def seed_teams(espn_teams: list[dict] | None = None) -> dict:
    """MLS entry point (kept so existing callers/tests are untouched)."""
    return seed_league_teams("mls-2026", ESPN_TEAMS, KALSHI_BRIDGES,
                             espn_teams=espn_teams)


def seed_league_teams(competition_slug: str, teams_url: str,
                      bridges: dict[str, str],
                      espn_teams: list[dict] | None = None) -> dict:
    """Idempotent, competition-keyed: insert missing clubs + approved
    aliases. `bridges` is the operator-curated Kalshi-name map for THIS
    competition. Returns counts. Never raises into the boot chain."""
    if not plane_ready():
        return {"skipped": "dormant"}
    if espn_teams is None:
        espn_teams = fetch_espn_teams(teams_url)
    added_teams = added_aliases = 0
    s = get_session()
    try:
        existing = {t.canonical_name: t for t in s.query(Team).filter_by(
            competition_slug=competition_slug)}
        for t in espn_teams:
            name = t.get("displayName")
            if not name or name in existing:
                continue
            row = Team(competition_slug=competition_slug,
                       canonical_name=name,
                       abbrev=t.get("abbreviation"),
                       espn_id=str(t.get("id")))
            s.add(row)
            s.flush()
            existing[name] = row
            added_teams += 1
            # ESPN self-aliases (displayName + shortDisplayName), approved
            for alias in {name, t.get("shortDisplayName")}:
                if alias:
                    if not s.query(TeamAlias).filter_by(
                            source="espn", alias=alias).first():
                        s.add(TeamAlias(team_id=row.id, alias=alias,
                                        source="espn", approved=True))
                        added_aliases += 1
        # curated Kalshi bridges -> approved aliases
        by_norm = {norm(t.canonical_name): t for t in existing.values()}
        for kalshi_name, espn_name in bridges.items():
            team = by_norm.get(norm(espn_name))
            if team is None:
                # tolerate ESPN name drift by containment either way
                team = next((t for n, t in by_norm.items()
                             if norm(espn_name) in n or n in norm(espn_name)),
                            None)
            if team is None:
                print(f"[identity] UNMAPPED bridge: {kalshi_name!r} -> "
                      f"{espn_name!r}")
                continue
            if not s.query(TeamAlias).filter_by(
                    source="kalshi", alias=kalshi_name).first():
                s.add(TeamAlias(team_id=team.id, alias=kalshi_name,
                                source="kalshi", approved=True))
                added_aliases += 1
        s.commit()
        total = s.query(Team).filter_by(
            competition_slug=competition_slug).count()
        return {"teams": total, "added_teams": added_teams,
                "added_aliases": added_aliases,
                "seeded_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        s.rollback()
        print(f"[identity] seed failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def resolve(source: str, alias: str,
            competition_slug: str | None = None) -> Team | None:
    """APPROVED aliases only — the decision's final-attachment rule.

    With `competition_slug` the resolved team must ALSO belong to that
    competition; a cross-competition hit fails EXPLICITLY (None + log)
    rather than silently attaching another league's club. Alias
    uniqueness is (source, alias) across the whole table, so once two
    leagues coexist an unscoped resolve could cross them (AGENTS.md §6:
    an ambiguous identity match must fail explicitly)."""
    s = get_session()
    if s is None:
        return None
    try:
        row = (s.query(TeamAlias)
               .filter_by(source=source, alias=alias, approved=True)
               .first())
        if row is None:
            return None
        team = s.get(Team, row.team_id)
        if (team is not None and competition_slug is not None
                and team.competition_slug != competition_slug):
            print(f"[identity] CROSS-COMPETITION alias refused: "
                  f"{source}:{alias!r} -> {team.canonical_name!r} "
                  f"({team.competition_slug}), wanted {competition_slug}")
            return None
        return team
    finally:
        s.close()


def resolve_espn_name(name: str,
                      competition_slug: str | None = None) -> Team | None:
    """ESPN display names are their own approved aliases."""
    return resolve("espn", name, competition_slug=competition_slug)


def unmapped_upcoming(names: list[str],
                      competition_slug: str | None = None) -> list[str]:
    """Readiness invariant helper: which of these ESPN names lack an
    approved mapping."""
    return [n for n in names
            if resolve_espn_name(n, competition_slug=competition_slug)
            is None]


# --- official MLS stats API (Sportec) identity ---------------------------
# Verified Jul 24 against the live season: all 30 Sportec
# `three_letter_code`s equal our ESPN `abbrev`s exactly (0 unmatched both
# ways), so a stats club resolves to our team by abbrev with no curation.
# A name-containment fallback is kept purely as defence against future
# expansion drift.

def resolve_mls_club(code: str, name: str | None = None) -> Team | None:
    """Resolve a Sportec club (its three-letter code, e.g. 'NSH') to our
    Team. Attachment requires a STABLE identifier: `abbrev == code` (the
    verified 1:1), or an approved TeamAlias. A name is never sufficient.

    TeamAlias already states the rule this enforces — fuzzy matching may
    only PROPOSE; attachment requires approved=True. The previous
    name-containment fallback broke it: it returned the FIRST substring
    match, so an MLS NEXT Pro reserve side ('Columbus Crew 2',
    'Austin FC II') resolved to its parent club. That does not fail
    loudly — it folds reserve-team stats into first-team xG ratings and
    into every lock downstream. AGENTS.md §6: an ambiguous identity match
    must fail explicitly, never silently pick a team.

    An unknown code returns None so the caller reports it observably
    (`mls_stats.ingest_match_stats` -> status 'unmapped_club'). Name
    near-misses are logged as curation candidates only; approving an
    alias is a deliberate act, never a side effect of a read."""
    if not code:
        return None
    s = get_session()
    if s is None:
        return None
    try:
        row = (s.query(Team)
               .filter_by(competition_slug="mls-2026")
               .filter(Team.abbrev == code).first())
        if row is not None:
            return row
        bridged = (s.query(TeamAlias)
                   .filter_by(source="mls_stats", alias=code, approved=True)
                   .first())
        if bridged is not None:
            return s.query(Team).filter_by(id=bridged.team_id).first()
        if name:
            n = norm(name)
            near = [t.canonical_name
                    for t in s.query(Team).filter_by(
                        competition_slug="mls-2026")
                    if n == norm(t.canonical_name)
                    or n in norm(t.canonical_name)
                    or norm(t.canonical_name) in n]
            if near:
                print(f"[identity] UNMAPPED mls club code={code!r} "
                      f"name={name!r} — name-only candidates NOT attached "
                      f"(approve an alias to bridge): {near}")
        return None
    finally:
        s.close()


def seed_mls_club_aliases(clubs: list[dict]) -> dict:
    """Record approved TeamAlias(source='mls_stats', alias=MLS-CLU-id) rows
    for provenance/audit. `clubs` = [{'sportec_id','code','name'}, ...].
    Ingestion resolves by code directly, so this is an auditable bridge,
    not a dependency. Idempotent; logs any club it cannot map."""
    if not plane_ready():
        return {"skipped": "dormant"}
    added = 0
    unmapped: list[str] = []
    s = get_session()
    try:
        for c in clubs:
            sportec_id = c.get("sportec_id")
            team = resolve_mls_club(c.get("code"), c.get("name"))
            if team is None or not sportec_id:
                unmapped.append(f"{c.get('code')}/{sportec_id}")
                continue
            if not s.query(TeamAlias).filter_by(
                    source="mls_stats", alias=sportec_id).first():
                s.add(TeamAlias(team_id=team.id, alias=sportec_id,
                                source="mls_stats", approved=True))
                added += 1
        s.commit()
        if unmapped:
            print(f"[identity] UNMAPPED mls_stats clubs: {unmapped}")
        return {"added_aliases": added, "unmapped": unmapped}
    except Exception as exc:
        s.rollback()
        print(f"[identity] mls club alias seed failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()
