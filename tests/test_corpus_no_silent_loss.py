"""Writing the prospective corpus must not be able to delete evidence.

WHAT HAPPENED. On 2026-08-07 this script, run exactly as documented,
wrote 6 rows over a corpus holding 11 — deleting every matchday-1 and
matchday-2 read. Those rows were the ONLY surviving record of those
reads: their pre-kickoff snapshots lived in session scratch and died
with their containers, the fragility #84 fixed forward and could not fix
backward. It was caught by diffing the output before staging it, which
is a habit, not a guarantee.

The corpus is prospective forecast evidence (AGENTS.md section 6), and
"never rewrite historical evidence" is a hard invariant. A tool whose
ordinary invocation silently drops it is a defect that no amount of care
fixes, because the next caller will not diff first.

So the default now REFUSES and names every row that would go, `--merge`
keeps them, `--replace` discards them out loud, and NO flag resolves a
conflict — two builds disagreeing about a settled score is not a retry.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_prospective_corpus",
        _ROOT / "scripts" / "build_prospective_corpus.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_prospective_corpus"] = m
    spec.loader.exec_module(m)
    return m


bpc = _load()


def _row(fid, home="A", away="B", goals=(1, 0), read=0.6,
         ko="2026-08-01T00:00:00+00:00"):
    return {"fixture_id": fid, "home": {"name": home}, "away": {"name": away},
            "_read": read, "kickoff_utc": ko,
            "goals": {"home": goals[0], "away": goals[1]},
            "final_status": "FT", "source_file": f"snap-{fid}.json"}


def _corpus(tmp_path, rows, name="corpus.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"n_rows": len(rows), "rows": rows}))
    return p


class TestTheDefaultRefuses:
    def test_it_will_not_delete_rows_the_new_build_does_not_cover(
            self, tmp_path):
        out = _corpus(tmp_path, [_row(1), _row(2), _row(3)])
        v = bpc.reconcile_with_existing(out, [_row(9)], merge=False,
                                        replace=False)
        assert "refused" in v
        assert "DELETE 3 row(s)" in v["refused"]

    def test_it_names_every_row_that_would_go(self, tmp_path):
        """A count is not actionable; a list is. The real incident lost
        eleven rows and the operator could only act because they were
        enumerated."""
        out = _corpus(tmp_path, [_row(1, "Charlotte"), _row(2, "Columbus")])
        v = bpc.reconcile_with_existing(out, [], merge=False, replace=False)
        names = [r["fixture"] for r in v["would_be_lost"]]
        assert names == ["Charlotte v B", "Columbus v B"]
        assert all(r["source_file"] for r in v["would_be_lost"])

    def test_the_refusal_says_why_rerunning_cannot_recreate_them(
            self, tmp_path):
        out = _corpus(tmp_path, [_row(1)])
        v = bpc.reconcile_with_existing(out, [], merge=False, replace=False)
        assert "session scratch" in v["why_this_matters"]
        assert "--merge" in v["means"] and "--replace" in v["means"]

    def test_no_default_is_offered_on_purpose(self, tmp_path):
        """Picking one silently would be a guess about irreplaceable
        evidence, which is the whole defect."""
        out = _corpus(tmp_path, [_row(1)])
        v = bpc.reconcile_with_existing(out, [], merge=False, replace=False)
        assert "no default" in v["means"].lower()


class TestWritingIsFineWhenNothingIsLost:
    def test_a_fresh_file_just_writes(self, tmp_path):
        v = bpc.reconcile_with_existing(tmp_path / "new.json", [_row(1)],
                                        merge=False, replace=False)
        assert v["write_mode"] == "created" and len(v["rows"]) == 1

    def test_a_superset_rewrite_is_not_a_loss(self, tmp_path):
        out = _corpus(tmp_path, [_row(1)])
        v = bpc.reconcile_with_existing(out, [_row(1), _row(2)], merge=False,
                                        replace=False)
        assert "refused" not in v
        assert v["write_mode"] == "rewrote_same_fixtures"
        assert len(v["rows"]) == 2


class TestMergeKeepsBoth:
    def test_merge_is_the_union_and_preserves_the_old_rows(self, tmp_path):
        out = _corpus(tmp_path, [_row(1), _row(2)])
        v = bpc.reconcile_with_existing(out, [_row(3)], merge=True,
                                        replace=False)
        assert v["write_mode"] == "merged"
        assert {r["fixture_id"] for r in v["rows"]} == {1, 2, 3}
        assert [p["fixture_id"] for p in v["preserved"]] == ["1", "2"]

    def test_merge_orders_by_kickoff_so_the_file_stays_readable(self,
                                                                 tmp_path):
        out = _corpus(tmp_path, [_row(1, ko="2026-08-05T00:00:00+00:00")])
        v = bpc.reconcile_with_existing(
            out, [_row(2, ko="2026-08-01T00:00:00+00:00")],
            merge=True, replace=False)
        assert [r["fixture_id"] for r in v["rows"]] == [2, 1]

    def test_merge_does_not_duplicate_an_overlapping_row(self, tmp_path):
        out = _corpus(tmp_path, [_row(1), _row(2)])
        v = bpc.reconcile_with_existing(out, [_row(2), _row(3)], merge=True,
                                        replace=False)
        assert len(v["rows"]) == 3


class TestReplaceIsLoud:
    def test_replace_discards_but_names_what_it_discarded(self, tmp_path):
        out = _corpus(tmp_path, [_row(1, "Gone")])
        v = bpc.reconcile_with_existing(out, [_row(9)], merge=False,
                                        replace=True)
        assert v["write_mode"] == "replaced"
        assert v["discarded"][0]["fixture"] == "Gone v B"
        assert [r["fixture_id"] for r in v["rows"]] == [9]


class TestAConflictIsNeverResolvedSilently:
    """Two builds disagreeing about a settled fact is not a retry — it
    means one is wrong. Letting argument order decide is how a corpus
    quietly acquires a false row (the journal's `corrects_bet_id` rule,
    one plane over)."""

    @pytest.mark.parametrize("field,changed", [
        ("goals", {"home": 9, "away": 9}),
        ("_read", 0.99),
        ("final_status", "AET"),
        ("kickoff_utc", "2020-01-01T00:00:00+00:00"),
    ])
    def test_a_changed_evidence_field_refuses(self, tmp_path, field, changed):
        out = _corpus(tmp_path, [_row(1)])
        new = _row(1)
        new[field] = changed
        v = bpc.reconcile_with_existing(out, [new], merge=False, replace=False)
        assert "refused" in v and v["conflicts"][0]["fields"] == [field]

    def test_merge_does_not_override_a_conflict(self, tmp_path):
        out = _corpus(tmp_path, [_row(1)])
        new = _row(1)
        new["goals"] = {"home": 9, "away": 9}
        v = bpc.reconcile_with_existing(out, [new], merge=True, replace=False)
        assert "refused" in v

    def test_replace_does_not_override_a_conflict_either(self, tmp_path):
        out = _corpus(tmp_path, [_row(1)])
        new = _row(1)
        new["goals"] = {"home": 9, "away": 9}
        v = bpc.reconcile_with_existing(out, [new], merge=False, replace=True)
        assert "refused" in v

    def test_an_identical_row_is_not_a_conflict(self, tmp_path):
        out = _corpus(tmp_path, [_row(1)])
        v = bpc.reconcile_with_existing(out, [_row(1)], merge=False,
                                        replace=False)
        assert "refused" not in v

    def test_a_non_evidence_field_changing_is_not_a_conflict(self, tmp_path):
        """`source_file` records which snapshot a row came from. Two
        snapshots can legitimately carry the same read."""
        out = _corpus(tmp_path, [_row(1)])
        new = _row(1)
        new["source_file"] = "a-different-snapshot.json"
        v = bpc.reconcile_with_existing(out, [new], merge=False,
                                        replace=False)
        assert "refused" not in v


class TestAnUnreadableCorpusRefusesRatherThanOverwrites:
    def test_corrupt_json_is_not_treated_as_an_empty_corpus(self, tmp_path):
        """The dangerous failure: unreadable -> assume nothing there ->
        overwrite. Then a parse error silently costs the whole file."""
        p = tmp_path / "corpus.json"
        p.write_text("{not json")
        v = bpc.reconcile_with_existing(p, [_row(1)], merge=False,
                                        replace=False)
        assert "refused" in v and "could not be read" in v["refused"]
        assert "do not guess" in v["means"]


class TestTheCliWiresItUp:
    def test_merge_and_replace_together_are_refused(self, tmp_path,
                                                     monkeypatch, capsys):
        snap = tmp_path / "s.json"
        snap.write_text(json.dumps({"fixtures": []}))
        monkeypatch.setattr(sys, "argv", [
            "x", "--snapshot", str(snap), "--out", str(tmp_path / "o.json"),
            "--merge", "--replace", "--results", str(snap)])
        assert bpc.main() == 2
        assert json.loads(capsys.readouterr().out)["error"] == \
            "merge_and_replace"

    def test_a_refusal_exits_2_and_leaves_the_file_untouched(
            self, tmp_path, monkeypatch, capsys):
        """Exit code, because a caller gates on it — and the bytes,
        because that is the actual promise."""
        out = _corpus(tmp_path, [_row(1), _row(2)])
        before = out.read_bytes()
        snap = tmp_path / "s.json"
        snap.write_text(json.dumps({"fixtures": []}))
        monkeypatch.setattr(sys, "argv", [
            "x", "--snapshot", str(snap), "--out", str(out),
            "--results", str(snap)])
        assert bpc.main() == 2
        assert out.read_bytes() == before
        assert json.loads(capsys.readouterr().out)["built"] is False
