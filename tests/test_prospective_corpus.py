"""The corpus builder, and the one property it exists to guarantee.

`measure_league_offset.py`'s clean arm is only clean because its reads
demonstrably predate their kickoffs. This builder is the sole thing
standing between that guarantee and a retrospective arm wearing a
prospective label — so every way a post-kickoff read could slip in is
asserted here rather than assumed:

  - a snapshot FILE written at or after kickoff, however innocent its
    contents look;
  - a snapshot whose fixture is already live or finished;
  - a fixture matched by name instead of by provider id;
  - an AET result, whose goals include extra time and therefore answer
    a different question than the read does;
  - a read that arrived LATER but is quietly discarded in favour of a
    staler one, which measures a number nobody acted on.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "build_prospective_corpus",
    _ROOT / "scripts" / "build_prospective_corpus.py")
bpc = importlib.util.module_from_spec(_SPEC)
sys.modules["build_prospective_corpus"] = bpc
_SPEC.loader.exec_module(bpc)

KO = "2026-08-05T23:30:00+00:00"
# derived from KO rather than pinned by hand — a literal here was wrong
# by 66 hours on the first run, and every clock assertion below is
# stated relative to this value
KO_EPOCH = int(datetime.fromisoformat(KO).timestamp())


def _fx(fid, home, away, status="NS", read=0.6, hg=None, ag=None, ko=KO):
    row = {"fixture_id": fid, "kickoff_utc": ko, "status": status,
           "home": {"name": home}, "away": {"name": away},
           "goals": {"home": hg, "away": ag}}
    if read is not None:
        row["strength"] = {"available": True,
                           "expected_points_share": {"home": read}}
    return row


def _write(tmp_path, name, fixtures, mtime):
    p = tmp_path / name
    p.write_text(json.dumps({"fixtures": fixtures}))
    os.utime(p, (mtime, mtime))
    return str(p)


class TestTheClockGuard:
    """The property the whole clean arm rests on."""

    def test_a_file_written_after_kickoff_is_refused(self, tmp_path):
        """Even though the payload claims NS — which this feed has in
        fact reported six minutes past a kickoff."""
        p = _write(tmp_path, "late.json", [_fx(1, "A", "B")],
                   KO_EPOCH + 600)
        best, rejects = bpc.collect_pre([p])
        assert best == {}
        assert "at or after kickoff" in rejects[0]["why"]

    def test_a_file_written_before_kickoff_is_admitted(self, tmp_path):
        p = _write(tmp_path, "early.json", [_fx(1, "A", "B")],
                   KO_EPOCH - 3600)
        best, _ = bpc.collect_pre([p])
        assert best["1"]["_read"] == 0.6
        assert best["1"]["lead_seconds"] == 3600

    def test_exactly_at_kickoff_is_refused(self, tmp_path):
        """The boundary belongs on the refusing side — a read captured
        at the whistle is not a pre-kickoff read."""
        p = _write(tmp_path, "on.json", [_fx(1, "A", "B")], KO_EPOCH)
        best, _ = bpc.collect_pre([p])
        assert best == {}

    def test_a_live_snapshot_is_refused_even_when_written_early(
            self, tmp_path):
        """Both conditions are required. A clock that says early cannot
        rescue a payload showing a scoreline."""
        p = _write(tmp_path, "live.json",
                   [_fx(1, "A", "B", status="1H", hg=1, ag=0)],
                   KO_EPOCH - 3600)
        best, rejects = bpc.collect_pre([p])
        assert best == {}
        assert "not pre-kickoff" in rejects[0]["why"]

    def test_an_NS_row_that_already_carries_goals_is_refused(self, tmp_path):
        p = _write(tmp_path, "odd.json",
                   [_fx(1, "A", "B", status="NS", hg=0, ag=0)],
                   KO_EPOCH - 3600)
        assert bpc.collect_pre([p])[0] == {}


class TestLatestAdmissibleWins:
    def test_the_read_closest_to_kickoff_is_kept(self, tmp_path):
        """The stale read is a number nobody acted on; the T-60 read is
        the one the briefing published and a correction would correct."""
        early = _write(tmp_path, "a.json", [_fx(1, "A", "B", read=0.50)],
                       KO_EPOCH - 40 * 3600)
        late = _write(tmp_path, "b.json", [_fx(1, "A", "B", read=0.61)],
                      KO_EPOCH - 3600)
        for order in ([early, late], [late, early]):
            best, _ = bpc.collect_pre(order)
            assert best["1"]["_read"] == 0.61, order
            assert best["1"]["lead_seconds"] == 3600

    def test_a_later_but_INADMISSIBLE_read_never_displaces_a_good_one(
            self, tmp_path):
        good = _write(tmp_path, "g.json", [_fx(1, "A", "B", read=0.61)],
                      KO_EPOCH - 3600)
        after = _write(tmp_path, "h.json", [_fx(1, "A", "B", read=0.99)],
                       KO_EPOCH + 60)
        best, _ = bpc.collect_pre([good, after])
        assert best["1"]["_read"] == 0.61


class TestIdentityAndCoverage:
    def test_a_fixture_without_a_provider_id_is_refused(self, tmp_path):
        row = _fx(None, "A", "B")
        del row["fixture_id"]
        p = _write(tmp_path, "x.json", [row], KO_EPOCH - 3600)
        best, rejects = bpc.collect_pre([p])
        assert best == {}
        assert "provider fixture id" in rejects[0]["why"]

    def test_two_clubs_with_the_same_names_are_distinct_by_id(self,
                                                              tmp_path):
        """Identity is the id, never names+date — the same clubs meet
        twice in this competition."""
        p = _write(tmp_path, "y.json",
                   [_fx(1, "A", "B", read=0.6),
                    _fx(2, "A", "B", read=0.4,
                        ko="2026-08-12T23:30:00+00:00")],
                   KO_EPOCH - 3600)
        best, _ = bpc.collect_pre([p])
        assert sorted(best) == ["1", "2"]

    def test_a_missing_read_costs_coverage_not_accuracy(self, tmp_path):
        p = _write(tmp_path, "z.json", [_fx(1, "A", "B", read=None)],
                   KO_EPOCH - 3600)
        best, rejects = bpc.collect_pre([p])
        assert best == {} and "no strength read" in rejects[0]["why"]

    def test_an_unavailable_strength_block_is_not_a_read(self, tmp_path):
        row = _fx(1, "A", "B")
        row["strength"] = {"available": False,
                           "expected_points_share": {"home": 0.6}}
        p = _write(tmp_path, "w.json", [row], KO_EPOCH - 3600)
        assert bpc.collect_pre([p])[0] == {}


class TestPairingWithOutcomes:
    def _run(self, tmp_path, pre_rows, result_rows, mtime=KO_EPOCH - 3600):
        snap = _write(tmp_path, "pre.json", pre_rows, mtime)
        res = tmp_path / "res.json"
        res.write_text(json.dumps({"fixtures": result_rows}))
        out = tmp_path / "corpus.json"
        sys.argv = ["x", "--snapshot", snap, "--results", str(res),
                    "--out", str(out)]
        assert bpc.main() == 0
        return json.loads(out.read_text())

    def test_a_settled_fixture_becomes_a_row_with_its_goals(self, tmp_path):
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)],
                        [_fx(1, "A", "B", status="FT", hg=2, ag=0)])
        assert doc["n_rows"] == 1
        r = doc["rows"][0]
        assert r["goals"] == {"home": 2, "away": 0}
        assert r["_read"] == 0.6 and r["final_status"] == "FT"

    def test_PEN_counts_and_carries_the_90_minute_score(self, tmp_path):
        """A shootout follows a 90-minute DRAW; the stored goals are the
        90-minute ones, which is what the read and the market answer."""
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)],
                        [_fx(1, "A", "B", status="PEN", hg=1, ag=1)])
        assert doc["n_rows"] == 1
        assert doc["rows"][0]["goals"] == {"home": 1, "away": 1}

    def test_AET_is_excluded_because_its_goals_include_extra_time(
            self, tmp_path):
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)],
                        [_fx(1, "A", "B", status="AET", hg=2, ag=1)])
        assert doc["n_rows"] == 0
        assert doc["pending_not_yet_settled"][0]["status"] == "AET"

    def test_an_unsettled_fixture_is_named_never_dropped(self, tmp_path):
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)],
                        [_fx(1, "A", "B", status="1H", hg=0, ag=0)])
        assert doc["n_rows"] == 0
        assert doc["pending_not_yet_settled"] == [
            {"fixture": "A v B", "status": "1H"}]

    def test_a_settled_row_with_no_score_does_not_become_a_loss(self,
                                                                tmp_path):
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)],
                        [_fx(1, "A", "B", status="FT")])
        assert doc["n_rows"] == 0
        assert "without a score" in \
            doc["pending_not_yet_settled"][0]["status"]

    def test_a_fixture_absent_from_results_is_named(self, tmp_path):
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)], [])
        assert doc["in_snapshot_but_absent_from_results"] == ["A v B"]

    def test_the_document_disclaims_what_it_is_not(self, tmp_path):
        doc = self._run(tmp_path, [_fx(1, "A", "B", read=0.6)],
                        [_fx(1, "A", "B", status="FT", hg=2, ag=0)])
        assert "not an edge claim" in doc["what_this_is_NOT"]
        assert "not a calibration" in doc["what_this_is_NOT"]
        assert doc["admissibility"]["file_mtime_before_kickoff"] is True

    def test_no_results_source_refuses(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("TRIVELA_API_BASE", raising=False)
        snap = _write(tmp_path, "p.json", [_fx(1, "A", "B")],
                      KO_EPOCH - 3600)
        monkeypatch.setattr(sys, "argv", ["x", "--snapshot", snap])
        assert bpc.main() == 2
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "no_results_source" and out["built"] is False


class TestTheMeasurementConsumesIt:
    """The builder's output must be readable by the arm it feeds —
    the two were written separately and the first real run found they
    disagreed about the document's shape."""

    def _offset(self):
        spec = importlib.util.spec_from_file_location(
            "measure_league_offset",
            _ROOT / "scripts" / "measure_league_offset.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["measure_league_offset"] = m
        spec.loader.exec_module(m)
        return m

    def test_the_wrapped_document_and_a_bare_list_both_load(self, tmp_path):
        lo = self._offset()
        rows = [{"home": {"name": "Columbus Crew"}, "away": {"name": "Atlas"},
                 "goals": {"home": 2, "away": 0}, "_read": 0.6}]
        rosters = {"mls": [(frozenset({"columbus", "crew"}), "Columbus Crew")],
                   "ligamx": [(frozenset({"atlas"}), "Atlas")]}
        bare = lo._arm(rows, rosters, "t")
        wrapped = lo._arm({"n_rows": 1, "rows": rows}["rows"], rosters, "t")
        assert bare == wrapped
        assert bare["by_host"]["mls_hosted"]["n"] == 1
