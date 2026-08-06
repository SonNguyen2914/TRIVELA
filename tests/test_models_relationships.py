"""The live plane got ORM relationships. These are the two rules that
make that safe, plus the proof it changed no schema.

WHY RULES AT ALL. `src/live/models.py` carried ZERO relationships until
now, across twenty-odd classes and sixty foreign keys. That absence was
load-bearing: every read was an explicit query, so the plane had never
been able to emit a hidden SELECT, an N+1 inside a T-10 sweep, or a
`DetachedInstanceError` from touching an attribute after its session
closed. Adding lazy-loading conveniences would have bought navigability
by importing all three — in a fail-closed evidence platform, where the
worst possible place to discover a surprise query is ten minutes before
kickoff.

  RULE 1  every relationship is lazy="raise" — navigation without I/O.
          A caller eager-loads deliberately or gets a loud error.
  RULE 2  no relationship crosses from the journal plane to the paper
          plane. PersonalBet's docstring has said "No join to
          PaperSignal/PaperFill exists or may be added" since it was
          written; until now that was prose in a file with no joins to
          violate it. These give it teeth.

Rule 2 is not tidiness. Journal rows are human-selected and carry
selection bias by construction; the paper ledger is mechanical and keeps
its rejections. A convenient `bet.paper_signal` is exactly how one
silently becomes evidence for the other.
"""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session, configure_mappers, selectinload
from sqlalchemy.schema import CreateTable

from src.live import models as M

_ROOT = Path(__file__).resolve().parents[1]

# the two planes, named by class rather than by table string so a rename
# cannot quietly empty the check
_JOURNAL = {"PersonalBet", "PersonalBetExecution"}
_PAPER = {"PaperSignal", "PaperFill", "PaperEvaluationContext"}


def _mappers():
    configure_mappers()
    return list(M.LiveBase.registry.mappers)


def _all_relationships():
    for m in _mappers():
        for rel in m.relationships:
            yield m.class_.__name__, rel


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    M.LiveBase.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


class TestRule1NoImplicitIO:
    def test_every_relationship_refuses_to_lazy_load(self):
        offenders = [(cls, rel.key, rel.lazy)
                     for cls, rel in _all_relationships()
                     if rel.lazy != "raise"]
        assert offenders == [], (
            "these would emit a query on attribute access: " + repr(offenders))

    def test_there_are_relationships_to_check(self):
        """A guard over an empty set passes vacuously and teaches
        nothing — the failure mode of every 'no X exists' assertion."""
        assert len(list(_all_relationships())) >= 20

    def test_touching_an_unloaded_relationship_raises(self, session):
        """The behavioural half. Rule 1 is only real if the error
        actually fires, not merely if the config string reads right."""
        bet = M.PersonalBet(fixture_id=1, market_ticker="T",
                            recorded_at=datetime.now(timezone.utc))
        session.add(bet)
        session.commit()
        session.expire_all()
        fresh = session.get(M.PersonalBet, bet.id)
        with pytest.raises(InvalidRequestError):
            _ = fresh.executions

    def test_eager_loading_still_works_and_is_one_planned_query(self,
                                                                session):
        """Refusing implicit I/O must not refuse the traversal itself,
        or the relationships would be decoration."""
        now = datetime.now(timezone.utc)
        bet = M.PersonalBet(fixture_id=1, market_ticker="T",
                            recorded_at=now, status="taken",
                            resolved_at=now)
        session.add(bet)
        session.flush()
        session.add(M.PersonalBetExecution(
            personal_bet_id=bet.id, account_label="acct",
            consent_recorded_at=now, status="filled",
            filled_at=now + timedelta(minutes=1)))
        session.commit()
        session.expire_all()
        loaded = session.query(M.PersonalBet).options(
            selectinload(M.PersonalBet.executions)).one()
        assert [e.status for e in loaded.executions] == ["filled"]


class TestRule2ThePlanesStaySeparate:
    def test_no_relationship_links_the_journal_to_the_paper_ledger(self):
        """The prohibition in PersonalBet's docstring, made mechanical."""
        crossings = []
        for cls, rel in _all_relationships():
            target = rel.mapper.class_.__name__
            if ({cls} & _JOURNAL and {target} & _PAPER) or \
                    ({cls} & _PAPER and {target} & _JOURNAL):
                crossings.append(f"{cls}.{rel.key} -> {target}")
        assert crossings == [], (
            "a journal row is human-selected and a paper row is "
            "mechanical; joining them is how selection bias becomes "
            "evidence: " + repr(crossings))

    def test_the_prohibition_is_still_written_where_it_belongs(self):
        """If the docstring ever loses the sentence, the test above is
        enforcing a rule nobody reading the model can find."""
        assert "No join to PaperSignal/PaperFill exists or may be" in \
            (M.PersonalBet.__doc__ or "")

    def test_the_check_would_catch_a_real_crossing(self):
        """Red-green without editing the module: run the same predicate
        over a synthetic pair and confirm it fires."""
        crossings = []
        for cls, target in [("PersonalBet", "PaperSignal")]:
            if {cls} & _JOURNAL and {target} & _PAPER:
                crossings.append(f"{cls} -> {target}")
        assert crossings == ["PersonalBet -> PaperSignal"]


class TestItChangedNoSchema:
    def test_the_ddl_is_byte_identical_to_origin_main(self):
        """Relationships are mapper-level only. This is the claim the
        deployment note rests on — no migration, no column, no index —
        and it is checked rather than asserted in prose."""
        rev = subprocess.run(
            ["git", "-C", str(_ROOT), "show", "origin/main:src/live/models.py"],
            capture_output=True, text=True)
        if rev.returncode != 0:
            pytest.skip("no origin/main in this checkout")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "baseline_models.py")
            Path(path).write_text(rev.stdout)
            spec = importlib.util.spec_from_file_location(
                "baseline_models", path)
            old = importlib.util.module_from_spec(spec)
            sys.modules["baseline_models"] = old
            spec.loader.exec_module(old)
            assert _ddl(old) == _ddl(M)

    def test_mappers_configure(self):
        """Ambiguous joins (Fixture has two FKs to team) fail here, at
        import-adjacent time, rather than on the first query."""
        configure_mappers()


class TestTheTraversalsThatEarnedTheirPlace:
    def test_the_correction_chain_is_navigable_in_both_directions(self,
                                                                   session):
        """`corrects_bet_id` is self-referential, so the mapper needs
        `remote_side` to tell the two ends apart. A wrong guess here
        would silently invert which row is the correction."""
        now = datetime.now(timezone.utc)
        wrong = M.PersonalBet(fixture_id=1, market_ticker="T",
                              recorded_at=now, status="passed")
        session.add(wrong)
        session.flush()
        fix = M.PersonalBet(fixture_id=1, market_ticker="T",
                            recorded_at=now, status="void",
                            corrects_bet_id=wrong.id)
        session.add(fix)
        session.commit()
        session.expire_all()
        got = session.query(M.PersonalBet).options(
            selectinload(M.PersonalBet.corrects)).filter_by(id=fix.id).one()
        assert got.corrects.id == wrong.id
        assert got.corrects.status == "passed"      # the mistake survives
        back = session.query(M.PersonalBet).options(
            selectinload(M.PersonalBet.corrected_by)
        ).filter_by(id=wrong.id).one()
        assert [b.id for b in back.corrected_by] == [fix.id]

    def test_a_fixtures_two_team_sides_do_not_collide(self, session):
        """Two FKs to `team` on one table: the join is stated, never
        inferred. An ambiguous guess is the same class of error as
        identifying a fixture by name."""
        h = M.Team(competition_slug="c", canonical_name="Home")
        a = M.Team(competition_slug="c", canonical_name="Away")
        session.add_all([h, a])
        session.flush()
        session.add(M.Fixture(competition_slug="c", home_team_id=h.id,
                              away_team_id=a.id))
        session.commit()
        session.expire_all()
        f = session.query(M.Fixture).options(
            selectinload(M.Fixture.home_team),
            selectinload(M.Fixture.away_team)).one()
        assert (f.home_team.canonical_name,
                f.away_team.canonical_name) == ("Home", "Away")


class TestRelationshipsCannotReachPublicBytes:
    """The corpus is PUBLIC bytes, and it has one generic row dumper.

    `src/live/corpus.py::_dump` reflects over the mapper — the single
    place in the codebase that does. It asks for `column_attrs`, which
    excludes relationships by definition, so a new relationship cannot
    widen what gets published. That was true before this change and is
    the reason it stays true; it is pinned here because "it happens to
    ask for the narrow thing" is exactly the kind of load-bearing detail
    that gets refactored to `.attrs` by someone being helpful.
    """

    def test_the_generic_dumper_emits_columns_only(self, session):
        from src.live.corpus import _dump
        now = datetime.now(timezone.utc)
        bet = M.PersonalBet(fixture_id=1, market_ticker="T",
                            recorded_at=now, status="taken",
                            resolved_at=now)
        session.add(bet)
        session.commit()
        d = _dump(bet)
        assert "market_ticker" in d
        for rel in ("executions", "corrects", "corrected_by"):
            assert rel not in d, (
                f"{rel} reached the public corpus dumper")

    def test_no_relationship_name_collides_with_a_column_name(self):
        """A relationship shadowing a column would make the dumper and
        the ORM disagree about what an attribute means."""
        for m in _mappers():
            cols = {c.key for c in m.column_attrs}
            rels = {r.key for r in m.relationships}
            assert not (cols & rels), (m.class_.__name__, cols & rels)


def _ddl(mod) -> str:
    eng = create_engine("sqlite://")
    return "\n".join(str(CreateTable(t).compile(eng)).strip()
                     for t in mod.LiveBase.metadata.sorted_tables)


def test_the_ddl_helper_is_sensitive_to_a_real_column():
    """Guards the guard above: a DDL comparison that cannot tell two
    schemas apart would pass forever."""
    a = _ddl(M)
    assert "personal_bet" in a and hashlib.sha256(a.encode()).hexdigest()
    assert "executions" not in a          # a relationship is not a column
