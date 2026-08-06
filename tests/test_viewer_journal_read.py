"""Viewer-competition journal rows were writable and unreadable.

`journal_pick.py record-viewer` writes entries against a viewer slug
(`leagues-cup-2026`). The only public journal route is pinned to
`mls-2026` by journal-P1 F9, so those rows had no read surface at all —
and `/api/mls/journal` answered `total_recorded: 15` on a store whose
live ids had passed 25. A number that looks like an answer and is scoped
to something else is worse than a refusal.

What must hold:

  - viewer rows become readable, per slug;
  - scopes are NEVER pooled — F9 exists so a second competition cannot
    inflate another's denominator, and that reason does not weaken just
    because both are viewer competitions;
  - slugs are DISCOVERED, not derived from key+season. `season_default`
    is None for most viewers and the provider's season label is not
    always the calendar year, so a derived slug would report an empty
    journal for rows that exist;
  - the MLS route is untouched — it is the surface an existing
    invariant is written against.
"""
import pytest
from fastapi.testclient import TestClient

from src.live import journal
from datetime import datetime, timezone

from src.live.models import Competition, Fixture, PersonalBet, Team
from tests import _livedb
from tests.test_mls_shadow import live_session  # noqa: F401


_NEXT = [1000]


def _seed(s, slug, n=2, status="considered", season=2026):
    """Idempotent per slug and re-callable: a test that seeds the same
    competition twice (taken rows, then passed rows) must not collide on
    Competition.slug or on fixture ids."""
    if s.get(Competition, slug) is None:
        _livedb.add_ordered(
            s, Competition(slug=slug, name=slug, season=season))
    base = _NEXT[0]
    _NEXT[0] += 100
    _livedb.add_ordered(
        s,
        Team(id=base, competition_slug=slug, canonical_name=f"H{base}"),
        Team(id=base + 1, competition_slug=slug, canonical_name=f"A{base}"))
    for i in range(n):
        fx = Fixture(id=base + 10 + i, competition_slug=slug,
                     provider_fixture_id=f"{base + i}", status="NS",
                     home_team_id=base, away_team_id=base + 1)
        s.add(fx)
        s.flush()
        s.add(PersonalBet(competition_slug=slug, fixture_id=fx.id,
                          market_ticker=f"T-{slug}-{i}",
                          outcome_key="home_win", status=status,
                          stated_price_dollars=0.5, stated_size=35,
                          recorded_at=datetime.now(timezone.utc),
                          price_basis="stated_only",
                          rationale="seed", content_hash=f"h{base}{i}"))
    s.commit()


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


class TestScopeDiscovery:
    def test_a_recorded_viewer_slug_is_found(self, live_session):
        _seed(live_session, "leagues-cup-2026")
        assert journal.journal_scopes("leagues-cup") == ["leagues-cup-2026"]

    def test_a_competition_with_no_rows_returns_nothing(self, live_session):
        _seed(live_session, "leagues-cup-2026")
        assert journal.journal_scopes("usl") == []

    def test_two_seasons_are_two_scopes_never_merged(self, live_session):
        """Different seasons are different denominators. Deriving one
        slug from key+season would have picked exactly one of these and
        silently reported the other as empty."""
        _seed(live_session, "leagues-cup-2025", n=3, season=2025)
        _seed(live_session, "leagues-cup-2026", n=2)
        assert journal.journal_scopes("leagues-cup") == [
            "leagues-cup-2025", "leagues-cup-2026"]

    def test_the_prefix_does_not_leak_across_competitions(self,
                                                          live_session):
        """`usl` must not match `usl-championship-2026` rows belonging to
        a different competition key — the separator is required."""
        _seed(live_session, "usl-2026")
        _seed(live_session, "uslx-2026")
        assert journal.journal_scopes("usl") == ["usl-2026"]

    def test_mls_rows_are_not_a_viewer_scope(self, live_session):
        _seed(live_session, "mls-2026")
        assert journal.journal_scopes("leagues-cup") == []


class TestTheRoute:
    def test_viewer_rows_are_now_readable(self, live_session, client):
        _seed(live_session, "leagues-cup-2026", n=2)
        r = client.get("/api/comp/leagues-cup/journal")
        assert r.status_code == 200
        body = r.json()
        assert body["scopes_found"] == ["leagues-cup-2026"]
        assert body["scopes"]["leagues-cup-2026"]["total_recorded"] == 2

    def test_scopes_are_reported_separately_and_never_summed(
            self, live_session, client):
        _seed(live_session, "leagues-cup-2025", n=3, season=2025)
        _seed(live_session, "leagues-cup-2026", n=2)
        body = client.get("/api/comp/leagues-cup/journal").json()
        totals = {k: v["total_recorded"] for k, v in body["scopes"].items()}
        assert totals == {"leagues-cup-2025": 3, "leagues-cup-2026": 2}
        assert "total_recorded" not in body        # no pooled number anywhere

    def test_an_empty_journal_says_so_rather_than_erroring(self, live_session,
                                                           client):
        body = client.get("/api/comp/usl/journal").json()
        assert body["scopes"] == {} and body["scopes_found"] == []
        assert "NOT that the journal is unavailable" in body["note"]

    def test_an_unknown_competition_is_404_not_an_empty_journal(self, client):
        assert client.get("/api/comp/not-a-league/journal").status_code == 404

    def test_the_counts_carry_the_denominator(self, live_session, client):
        """considered/taken/passed together, exactly as the MLS surface
        reports them — a hit rate over taken alone is not a statistic."""
        _seed(live_session, "leagues-cup-2026", n=2, status="taken")
        _seed(live_session, "leagues-cup-2026", n=1, status="passed")
        c = (client.get("/api/comp/leagues-cup/journal")
             .json()["scopes"]["leagues-cup-2026"]["counts"])
        assert c["taken"] == 2 and c["passed"] == 1

    def test_no_aggregate_appears_below_the_floor(self, live_session,
                                                   client):
        _seed(live_session, "leagues-cup-2026", n=2, status="taken")
        sc = (client.get("/api/comp/leagues-cup/journal")
              .json()["scopes"]["leagues-cup-2026"])
        assert "aggregate_withheld" in sc
        for banned in ("roi", "hit_rate", "clv", "pnl"):
            assert banned not in sc


class TestTheMlsSurfaceIsUnchanged:
    def test_mls_journal_still_reports_only_mls(self, live_session, client):
        """F9 is the invariant this fix must not weaken: a viewer
        competition's rows must stay out of the MLS denominator."""
        _seed(live_session, "mls-2026", n=2)
        _seed(live_session, "leagues-cup-2026", n=4)
        body = client.get("/api/mls/journal").json()
        assert body["total_recorded"] == 2
