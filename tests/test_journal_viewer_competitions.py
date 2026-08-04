"""The journal could only ever record MLS.

A pick had to name a `fixture.id`, and only the MLS ingest creates those.
So Leagues Cup ran live this week — 54 booked matches, real Kalshi books —
with no way to record a single pick (journal-P0-M). The briefing surfaces
address a viewer match by `(competition, provider fixture id)`; the write
path now accepts the same pair.

WHAT THIS MUST NOT DO, and each is asserted rather than assumed:
  - change MLS behaviour in any way (the fixture-id path is untouched)
  - pretend a created row is ingest — no model run, no market chain, so a
    viewer entry can only be `stated_only` and counts nowhere
  - let the scorer score what it cannot score, or drop it silently
  - invent a competition season it cannot read
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.live import journal
from src.live.models import (Competition, Fixture, PersonalBet, Team)
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc
SLUG = "leagues-cup-2026"
PID = "1530108"
TICK = "KXLEAGUESCUPGAME-26AUG04CLBATL-CLB"


def _record(**over):
    body = dict(fixture_id=None, market_ticker=TICK,
                outcome_key="home_win", competition_slug=SLUG,
                provider_fixture_id=PID,
                kickoff_utc=datetime.now(UTC) + timedelta(hours=6),
                home_team="Columbus Crew", away_team="Atlas",
                stated_price="0.52", rationale="matchday 1")
    body.update(over)
    fixture_id = body.pop("fixture_id")
    ticker = body.pop("market_ticker")
    return journal.record_view(fixture_id, ticker, **body)


class TestAViewerPickCanBeRecordedAtAll:
    def test_a_leagues_cup_pick_is_recorded(self, live_session):
        out = _record()
        assert "error" not in out, out
        assert out["id"]
        assert out["status"] == "considered"
        assert out["market_ticker"] == TICK

    def test_it_creates_the_competition_and_fixture_once(self, live_session):
        first = _record()
        second = _record(stated_price="0.55", rationale="second view")
        assert "error" not in second, second
        assert live_session.query(Competition).filter_by(slug=SLUG).count() == 1
        fixtures = live_session.query(Fixture).filter_by(
            competition_slug=SLUG, provider_fixture_id=PID).all()
        assert len(fixtures) == 1, "get-or-create must not duplicate"
        assert first["fixture_id"] == second["fixture_id"]

    def test_teams_are_stored_as_named_in_the_shared_namespace(
            self, live_session):
        _record()
        names = {t.canonical_name for t in
                 live_session.query(Team).filter_by(competition_slug=SLUG)}
        assert names == {"Columbus Crew", "Atlas"}

    def test_the_kickoff_is_stored_and_drives_the_void_rule(
            self, live_session):
        """After kickoff a view is not a forecast — the rule must apply to
        viewer competitions exactly as it does to MLS."""
        out = _record(kickoff_utc=datetime.now(UTC) - timedelta(hours=1))
        assert out["status"] == "void"


class TestItCannotPretendToBeIngest:
    def test_a_viewer_entry_is_stated_only_and_counts_nowhere(
            self, live_session):
        """No approved market chain exists for a viewer competition, so
        there is nothing to grant observed_quote against."""
        out = _record()
        assert out["price_basis"] == "stated_only"
        assert out["counts_toward_aggregate"] is False

    def test_no_model_probability_is_invented(self, live_session):
        """These competitions have no model by design; a number here
        would be fabricated."""
        out = _record()
        assert out["model_probability"] is None
        assert out["prediction_run_id"] is None

    def test_citing_a_foreign_quote_is_still_refused(self, live_session):
        """The identity guard must not weaken just because the fixture
        was created on the fly."""
        from tests.test_personal_journal import _seed
        _seed(live_session)                      # MLS fixture 1, quote 1
        out = _record(market_quote_id=1)
        assert "error" in out
        assert "belongs to fixture 1" in out["error"]


class TestItRefusesWhatItCannotKnow:
    def test_an_unseeded_competition_without_a_year_is_refused(
            self, live_session):
        out = _record(competition_slug="leagues-cup")
        assert "error" in out
        assert "season cannot be read" in out["error"]

    def test_a_missing_provider_id_is_refused(self, live_session):
        out = _record(provider_fixture_id=None)
        assert "error" in out
        assert "provider_fixture_id" in out["error"]

    def test_a_missing_competition_slug_is_refused(self, live_session):
        out = _record(competition_slug=None)
        assert "error" in out
        assert "competition_slug" in out["error"]

    def test_an_already_seeded_competition_is_reused_not_reinvented(
            self, live_session):
        live_session.add(Competition(slug=SLUG, name="Leagues Cup",
                                     season=2026))
        live_session.commit()
        _record()
        comp = live_session.get(Competition, SLUG)
        assert comp.name == "Leagues Cup", "must not overwrite a real seed"


class TestMLSIsByteIdentical:
    def test_the_fixture_id_path_is_unchanged(self, live_session):
        from tests.test_personal_journal import _seed
        _seed(live_session)
        out = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1, stated_price="0.45")
        assert out["price_basis"] == "observed_quote"
        assert out["model_probability"] == 0.52

    def test_an_unknown_fixture_id_still_says_no_such_fixture(
            self, live_session):
        """The MLS error must not be replaced by a create-on-the-fly."""
        out = journal.record_view(4242, "KXMLSGAME-x-H",
                                  outcome_key="home_win")
        assert out == {"error": "no such fixture"}
        assert live_session.query(Fixture).count() == 0

    def test_an_id_wins_over_provider_addressing(self, live_session):
        """Both supplied: the id path runs and nothing is created."""
        from tests.test_personal_journal import _seed
        _seed(live_session)
        out = journal.record_view(
            1, "KXMLSGAME-x-H", outcome_key="home_win",
            competition_slug=SLUG, provider_fixture_id=PID)
        assert "error" not in out
        assert out["fixture_id"] == 1
        assert live_session.query(Competition).filter_by(
            slug=SLUG).count() == 0


class TestTheScorerStillRefusesByName:
    def test_a_viewer_entry_is_excluded_and_named(self, live_session):
        """It must never be scored, and must never vanish quietly."""
        from src.live import scorer
        out = _record()
        bet = live_session.get(PersonalBet, out["id"])
        assert bet.competition_slug == SLUG
        scored = scorer.score(competition_slug=SLUG)
        assert scored["countable"] == 0
        assert scored["excluded"] == {"stated_only": 1}
        assert scored["corpus"] == "empty"

    def test_the_mls_scorer_never_sees_it(self, live_session):
        from src.live import scorer
        _record()
        assert scorer.score()["excluded"] == {}


class TestOverTheRoute:
    def test_the_api_accepts_the_viewer_shape(self, live_session,
                                              monkeypatch):
        import config
        from fastapi.testclient import TestClient
        from api import main as api_main
        monkeypatch.setattr(config, "PUBLIC_READ_ONLY", True)
        monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
        monkeypatch.setattr(config, "JOURNAL_TOKEN", "journal-secret")
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
        c = TestClient(api_main.app, raise_server_exceptions=False)
        r = c.post("/api/admin/mls/journal/view",
                   headers={"X-Journal-Token": "journal-secret"},
                   json={"market_ticker": TICK, "outcome_key": "home_win",
                         "competition_slug": SLUG,
                         "provider_fixture_id": PID,
                         "kickoff_utc": (datetime.now(UTC)
                                         + timedelta(hours=6)).isoformat(),
                         "home_team": "Columbus Crew", "away_team": "Atlas",
                         "stated_price": "0.52"})
        assert r.status_code == 200, r.text
        assert r.json()["price_basis"] == "stated_only"

    def test_omitting_everything_is_a_4xx_not_a_500(self, live_session,
                                                    monkeypatch):
        import config
        from fastapi.testclient import TestClient
        from api import main as api_main
        monkeypatch.setattr(config, "PUBLIC_READ_ONLY", True)
        monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
        monkeypatch.setattr(config, "JOURNAL_TOKEN", "journal-secret")
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
        c = TestClient(api_main.app, raise_server_exceptions=False)
        r = c.post("/api/admin/mls/journal/view",
                   headers={"X-Journal-Token": "journal-secret"},
                   json={"market_ticker": TICK, "outcome_key": "home_win"})
        assert r.status_code == 400, r.text
        assert "required" in r.json()["error"]
