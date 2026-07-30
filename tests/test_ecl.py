"""Conference League: a cup, and the refusal to model it.

The load-bearing test here is not that fixtures parse — it is that this
surface never grows a model. The measured reason it cannot have one (median
4 matches per club, 53 of 164 above the floor) is a permanent property of a
four-round qualifying cup, so a future contributor adding a fitted rating
here would be building on sand.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from src import ecl


class TestNoModelByDesign:
    def test_status_states_the_measured_reason(self):
        m = ecl.NO_MODEL_REASON
        assert m["state"] == "no_model_by_design"
        meas = m["measured"]
        # the numbers that justify the refusal travel WITH it
        assert meas["clubs"] == 164
        assert meas["median_matches_per_club"] == 4
        assert meas["clubs_at_or_above_min_games"] == 53
        assert meas["min_games"] == 5
        assert meas["clubs_at_or_above_min_games"] < meas["clubs"] / 2

    def test_the_floor_matches_the_real_model_constant(self):
        """If MIN_GAMES ever changes, this justification must be re-measured
        rather than silently becoming wrong."""
        from src.live import model_mls
        assert ecl.NO_MODEL_REASON["measured"]["min_games"] == \
            model_mls.MIN_GAMES

    def test_module_never_imports_model_or_approval_machinery(self):
        """Structural. This competition must not acquire a fitted model or
        an approval decision by drift."""
        root = pathlib.Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "src/ecl.py").read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                names |= {a.name for a in n.names}
                if isinstance(n, ast.ImportFrom) and n.module:
                    names.add(n.module)
        forbidden = {"model_eval", "ModelApprovalDecision", "model_mls",
                     "ladder_replay", "runs"}
        assert not (names & forbidden), f"ecl imports {names & forbidden}"

    def test_the_forbidden_names_exist(self):
        """Control: a scan for names that were renamed proves nothing."""
        from src.live import ladder_replay, model_eval
        assert hasattr(model_eval, "ladder_specs")
        assert hasattr(ladder_replay, "REPLAY_SOURCES")

    def test_framing_never_claims_a_forecast(self):
        text = (ecl.FRAMING + " " + str(ecl.NO_MODEL_REASON)).lower()
        for word in ("prediction", "forecast our", "fair value",
                     "expected winner", "advice"):
            assert word not in text, f"banned vocabulary: {word!r}"
        assert "no model" in text


class TestKalshiSeriesWasProbedNotGuessed:
    def test_the_ticker_is_the_probed_one(self):
        """KXUECLGAME returned 200 on 2026-07-30 while KXECLGAME,
        KXCONFGAME and KXUEFACONFGAME all 404'd — the ticker is not
        derivable from the competition's name, so it must be the probed
        value and not a plausible-looking guess."""
        assert ecl.KALSHI_GAME_SERIES == "KXUECLGAME"

    def test_markets_distinguishes_listed_from_tradeable(self, monkeypatch):
        """The EPL lesson: status=open returned ten events that were all
        settled fixtures from the prior season. Both counts, always."""
        from src import friendlies
        monkeypatch.setattr(
            friendlies, "_get_json",
            lambda url, params=None: {"events": [
                {"event_ticker": "A", "markets": [{"status": "active"}]},
                {"event_ticker": "B", "markets": [{"status": "settled"}]},
            ]})
        ecl._cache.clear()
        m = ecl.markets()
        assert m["listed_events"] == 2
        assert m["tradeable_events"] == 1

    def test_a_failed_registry_read_is_not_no_books(self, monkeypatch):
        from src import friendlies
        monkeypatch.setattr(friendlies, "_get_json",
                            lambda url, params=None: None)
        ecl._cache.clear()
        m = ecl.markets()
        assert m["status"] == "unavailable"
        assert "UNKNOWN" in m["means"]
        assert "tradeable_events" not in m       # no count is claimed


class TestFixturesCarryTheRound:
    def test_round_is_parsed_from_the_provider(self):
        """Cup rounds are load-bearing context, and parse_fixture had to be
        extended to keep them — it previously dropped the field, so every
        ECL row read `round: None`."""
        from src.friendlies_apif import parse_fixture
        row = {
            "fixture": {"id": 1, "date": "2026-08-01T18:00:00+00:00",
                        "status": {"short": "NS", "long": "Not Started"},
                        "venue": {"name": "V", "city": "C"}},
            "league": {"id": ecl.APIF_LEAGUE_ID, "name": "ECL",
                       "season": 2026, "country": "World",
                       "round": "2nd Qualifying Round"},
            "teams": {"home": {"id": 1, "name": "H", "logo": "h"},
                      "away": {"id": 2, "name": "A", "logo": "a"}},
            "goals": {"home": None, "away": None},
        }
        p = parse_fixture(row)
        assert p is not None
        assert p["round"] == "2nd Qualifying Round"
        assert p["venue"] == "V"
        assert p["home"]["crest"] == "h"
