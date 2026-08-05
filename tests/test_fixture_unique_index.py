"""The (competition_slug, provider_fixture_id) unique index — the
database-level answer to the #72 review's known gap ("get-or-create is
application-level and racy").

Per the test-DB-parity rule: the index is declared on the MODEL, so
SQLite enforces here exactly what the migration enforces on PostgreSQL,
and the guard is proven by attempting the very duplicate it forbids.
"""
import pytest
from sqlalchemy.exc import IntegrityError

from src.live.models import Competition, Fixture
from tests.test_mls_shadow import live_session  # noqa: F401


def _fx(slug, pid):
    return Fixture(competition_slug=slug, provider_fixture_id=pid)


class TestUniqueIndex:
    def test_a_duplicate_pair_is_refused_by_the_database(
            self, live_session):
        live_session.add(Competition(slug="leagues-cup-2026",
                                     name="Leagues Cup", season=2026))
        live_session.add(_fx("leagues-cup-2026", "1530108"))
        live_session.commit()
        live_session.add(_fx("leagues-cup-2026", "1530108"))
        with pytest.raises(IntegrityError):
            live_session.commit()
        live_session.rollback()

    def test_the_same_provider_id_in_another_competition_is_fine(
            self, live_session):
        for slug in ("leagues-cup-2026", "asean-2025"):
            live_session.add(Competition(slug=slug, name=slug, season=2026))
            live_session.add(_fx(slug, "777"))
        live_session.commit()

    def test_null_provider_ids_stay_unlimited(self, live_session):
        # the MLS plane's own fixtures carry NULL provider ids forever —
        # the index is partial precisely so they never collide
        live_session.add(Competition(slug="nullcase-2026", name="NullCase",
                                     season=2026))
        live_session.add(_fx("nullcase-2026", None))
        live_session.add(_fx("nullcase-2026", None))
        live_session.commit()
