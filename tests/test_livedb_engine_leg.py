"""The engine leg is real, visible, and enforced by the engine itself.

TASK-11's failure mode is a false green: PG_TEST_URL set, CI titled
"postgres", every test quietly still on SQLite. Each test here would
catch that lie a different way — the dialect on the live connection,
the leg counter, and a constraint only a real PostgreSQL can enforce.

Red-green: with PG_TEST_URL set, all three FAILED against the pre-TASK-11
fixtures (which hardcoded sqlite) and pass after parameterization. With
it unset they pass before and after — the SQLite leg is the control.
"""
import pytest

from tests import _livedb
from tests.test_mls_shadow import live_session  # noqa: F401

EXPECTED = "postgresql" if _livedb.PG_URL else "sqlite"


class TestTheLegIsWhatTheEnvironmentSays:
    def test_the_live_session_runs_on_the_declared_engine(self, live_session):
        assert live_session.bind.dialect.name == EXPECTED

    def test_the_leg_counter_saw_this_provision(self, live_session):
        """`postgresql=0` at the end of a PG run is the visible form of
        the silent skip this task exists to prevent."""
        assert _livedb.LEGS[EXPECTED] >= 1

    def test_the_simulation_flag_matches_the_engine(self):
        """The varchar imitation exists FOR sqlite; running it on the
        PG leg would mask real-engine divergence."""
        assert _livedb.SIMULATE_VARCHAR == (EXPECTED == "sqlite")


class TestTheConstraintIsEnforcedByTheEngineItself:
    def test_an_overlong_varchar_is_refused_per_engine(self, live_session):
        """Fixture.status is String(16). SQLite ignores the length, so
        the suite simulates PostgreSQL's refusal in a before_flush
        guard (ValueError). On the real PG leg the guard is OFF and the
        ENGINE must refuse — sqlalchemy wraps StringDataRightTruncation
        in DataError, which no simulation can produce. Whichever leg
        runs, an overlong value must not survive; HOW it dies proves
        which engine was underneath."""
        from sqlalchemy.exc import DataError
        from src.live.models import Fixture

        live_session.add(Fixture(
            competition_slug="mls-2026", espn_event_id="proof-1",
            status="x" * 40))
        if EXPECTED == "postgresql":
            with pytest.raises(DataError):
                live_session.commit()
        else:
            with pytest.raises(ValueError, match="value too long"):
                live_session.commit()
        live_session.rollback()

    def test_a_legal_value_commits_on_either_leg(self, live_session):
        from src.live.models import Fixture
        live_session.add(Fixture(
            competition_slug="mls-2026", espn_event_id="proof-2",
            status="pre"))
        live_session.commit()
        assert live_session.query(Fixture).filter_by(
            espn_event_id="proof-2").count() == 1
