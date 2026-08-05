"""The ESPN lineup bridge for viewer competitions.

The bridge carries the three guards the sharp-anchor bridge earned
(#74) — both clubs, a kickoff window, reverse uniqueness — because a
name-plus-date join is the only join available here and those are the
failures it is known to produce.
"""
import pytest

from src.live import espn_lineups as el


def _ev(eid, home, away, date):
    return {"id": eid, "date": date,
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"displayName": home}},
                {"homeAway": "away", "team": {"displayName": away}}]}]}


def _row(fid, home, away, ko):
    return {"fixture_id": fid, "kickoff_utc": ko,
            "home": {"name": home}, "away": {"name": away}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(el, "_board", {})


def _wire(monkeypatch, events):
    monkeypatch.setattr(el, "_events", lambda slug, day: events)


class TestSlugs:
    def test_every_slug_was_probed_and_commented(self):
        # each entry carries the league name ESPN itself reported
        assert el.ESPN_SLUGS["leagues-cup"] == "concacaf.leagues.cup"
        assert el.ESPN_SLUGS["asean"] == "aff.championship"

    def test_an_unmapped_competition_refuses_by_name(self):
        m, un = el.bridge([_row(1, "A", "B", "2026-08-05T23:30:00+00:00")],
                          "nosuchcomp")
        assert m == {}
        assert "no probed ESPN slug" in un[0]["reason"]


class TestBridge:
    KO = "2026-08-06T00:30:00+00:00"

    def test_a_clean_pair_bridges(self, monkeypatch):
        _wire(monkeypatch, [_ev("777", "FC Dallas", "Querétaro",
                                "2026-08-06T00:30Z")])
        m, un = el.bridge([_row(1, "FC Dallas", "Club Queretaro", self.KO)],
                          "leagues-cup")
        assert m == {1: "777"} and un == []

    def test_reversed_orientation_still_bridges(self, monkeypatch):
        # ESPN lists "Querétaro at FC Dallas"; sides resolve by name later
        _wire(monkeypatch, [_ev("777", "Querétaro", "FC Dallas",
                                "2026-08-06T00:30Z")])
        m, _ = el.bridge([_row(1, "FC Dallas", "Club Queretaro", self.KO)],
                         "leagues-cup")
        assert m == {1: "777"}

    def test_one_matching_club_is_not_enough(self, monkeypatch):
        _wire(monkeypatch, [_ev("777", "FC Dallas", "Houston Dynamo",
                                "2026-08-06T00:30Z")])
        m, un = el.bridge([_row(1, "FC Dallas", "Club Queretaro", self.KO)],
                          "leagues-cup")
        assert m == {}
        assert "both clubs" in un[0]["reason"]

    def test_the_same_pairing_a_week_later_is_out_of_window(
            self, monkeypatch):
        _wire(monkeypatch, [_ev("777", "FC Dallas", "Querétaro",
                                "2026-08-13T00:30Z")])
        m, un = el.bridge([_row(1, "FC Dallas", "Club Queretaro", self.KO)],
                          "leagues-cup")
        assert m == {}
        assert "window" in un[0]["reason"]

    def test_one_event_claimed_twice_refuses_both(self, monkeypatch):
        _wire(monkeypatch, [_ev("777", "FC Dallas", "Querétaro",
                                "2026-08-06T00:30Z")])
        m, un = el.bridge([_row(1, "FC Dallas", "Club Queretaro", self.KO),
                           _row(2, "FC Dallas", "Club Queretaro", self.KO)],
                          "leagues-cup")
        assert m == {}
        assert len(un) == 2
        assert all("refused" in u["reason"] for u in un)

    def test_the_lafc_alias_rescues_an_initialism(self, monkeypatch):
        # ESPN abbreviates to "LAFC" — zero token overlap with ours, so
        # the rule correctly refused until a PINNED alias was added
        _wire(monkeypatch, [_ev("778", "LAFC", "Guadalajara",
                                "2026-08-06T02:30Z")])
        m, un = el.bridge(
            [_row(3, "Los Angeles FC", "Guadalajara Chivas",
                  "2026-08-06T02:30:00+00:00")], "leagues-cup")
        assert m == {3: "778"}, un

    def test_a_failed_scoreboard_is_not_read_as_no_matches(self,
                                                           monkeypatch):
        monkeypatch.setattr(el, "_events", lambda slug, day: None)
        m, un = el.bridge([_row(1, "FC Dallas", "Club Queretaro", self.KO)],
                          "leagues-cup")
        assert m == {}
        assert un and "no ESPN event matches" in un[0]["reason"]


class TestCapture:
    def test_an_unmapped_competition_refuses(self, monkeypatch):
        from src.live import team_news as tn
        monkeypatch.setattr(tn, "plane_ready", lambda: True)
        out = el.capture(1, "777", "nosuchcomp")
        assert out["state"] == "unavailable"
        assert "no probed ESPN slug" in out["note"]

    def test_a_dormant_plane_refuses_without_touching_the_provider(
            self, monkeypatch):
        from src.live import team_news as tn
        monkeypatch.setattr(tn, "plane_ready", lambda: False)

        def boom(*a, **k):
            raise AssertionError("provider called on a dormant plane")
        monkeypatch.setattr(tn, "_espn_summary", boom)
        assert el.capture(1, "777", "leagues-cup")["state"] == "dormant"
