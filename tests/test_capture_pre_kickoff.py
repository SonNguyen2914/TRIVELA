"""Making a snapshot survive, and the trap that made "just commit it"
insufficient.

Both 2026 slates were captured only into a session scratch directory
that dies with its container; the corpus for them survived by luck. The
obvious fix — write it somewhere tracked and commit it — silently
breaks the corpus builder, because **git does not preserve mtime**: a
committed snapshot returns from a clone with an mtime of the checkout,
which is after every past kickoff, so the builder's pre-kickoff
condition would reject every row in it.

So the capture writes `captured_at` into the document and the builder
prefers it. These assert both halves, plus the properties that keep a
capture worth citing: immutability, and saying at capture time what the
snapshot will and will not contribute.
"""
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


cap = _load("capture_pre_kickoff")
bpc = _load("build_prospective_corpus")

KO = "2026-08-06T23:30:00+00:00"
KO_EPOCH = int(datetime.fromisoformat(KO).timestamp())


def _fx(fid, home, away, status="NS", read=0.6, hg=None, ag=None, ko=KO):
    row = {"fixture_id": fid, "kickoff_utc": ko, "status": status,
           "home": {"name": home}, "away": {"name": away},
           "goals": {"home": hg, "away": ag}}
    if read is not None:
        row["strength"] = {"available": True,
                           "expected_points_share": {"home": read}}
    return row


def _run(monkeypatch, tmp_path, fixtures, now, extra=()):
    body = {"competition": "leagues-cup", "fixtures": fixtures}
    monkeypatch.setattr(cap.urllib.request, "urlopen",
                        lambda u: io.BytesIO(json.dumps(body).encode()))
    argv = ["x", "--competition", "leagues-cup", "--api-base",
            "https://x.test", "--dir", str(tmp_path), "--now", now, *extra]
    monkeypatch.setattr(sys, "argv", argv)
    return cap.main()


class TestTheWitnessSurvivesGit:
    """The whole reason this pair of changes exists."""

    def test_captured_at_is_written_into_the_document(self, monkeypatch,
                                                       tmp_path):
        rc = _run(monkeypatch, tmp_path, [_fx(1, "A", "B")],
                  "2026-08-06T22:30:00+00:00")
        assert rc == 0
        doc = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert doc["captured_at"] == "2026-08-06T22:30:00+00:00"
        assert "git does not preserve mtime" in \
            doc["captured_at_is_the_witness"].lower()

    def test_a_checkout_style_mtime_does_NOT_reject_the_rows(
            self, monkeypatch, tmp_path):
        """This is the exact failure a committed snapshot would hit: the
        file's mtime is NOW (after kickoff) while captured_at is an hour
        BEFORE it. Without the in-document witness every row is lost."""
        _run(monkeypatch, tmp_path, [_fx(1, "A", "B")],
             "2026-08-06T22:30:00+00:00")
        p = next(tmp_path.glob("*.json"))
        os.utime(p, (KO_EPOCH + 99999, KO_EPOCH + 99999))   # "git checkout"
        best, rejects = bpc.collect_pre([str(p)])
        assert list(best) == ["1"], rejects
        assert best["1"]["captured_at_witness"] == "captured_at"
        assert best["1"]["lead_seconds"] == 3600

    def test_mtime_is_still_used_when_no_captured_at_exists(self, tmp_path):
        """Snapshots taken before this change have no such field and must
        keep working."""
        p = tmp_path / "legacy.json"
        p.write_text(json.dumps({"fixtures": [_fx(1, "A", "B")]}))
        os.utime(p, (KO_EPOCH - 3600, KO_EPOCH - 3600))
        best, _ = bpc.collect_pre([str(p)])
        assert best["1"]["captured_at_witness"] == "file_mtime"

    def test_a_captured_at_after_kickoff_still_rejects(self, monkeypatch,
                                                       tmp_path):
        """The in-document witness must not become a way to smuggle a
        post-kickoff read in — it replaces mtime, it does not excuse the
        condition."""
        p = tmp_path / "late.json"
        p.write_text(json.dumps({
            "captured_at": "2026-08-06T23:45:00+00:00",
            "fixtures": [_fx(1, "A", "B")]}))
        os.utime(p, (KO_EPOCH - 99999, KO_EPOCH - 99999))   # innocent mtime
        best, rejects = bpc.collect_pre([str(p)])
        assert best == {}
        assert "witness: captured_at" in rejects[0]["why"]

    def test_the_corpus_document_explains_the_witness(self, monkeypatch,
                                                       tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"captured_at": "2026-08-06T22:30:00+00:00",
                                 "fixtures": [_fx(1, "A", "B")]}))
        res = tmp_path / "r.json"
        res.write_text(json.dumps({"fixtures": [
            _fx(1, "A", "B", status="FT", hg=2, ag=0)]}))
        out = tmp_path / "c.json"
        monkeypatch.setattr(sys, "argv", [
            "x", "--snapshot", str(p), "--results", str(res),
            "--out", str(out)])
        assert bpc.main() == 0
        doc = json.loads(out.read_text())
        w = doc["admissibility"]["capture_time_witness"]
        assert "GIT DOES NOT" in w and "commit time" in w


class TestACaptureIsImmutable:
    def test_an_existing_path_is_never_overwritten(self, monkeypatch,
                                                    tmp_path, capsys):
        now = "2026-08-06T22:30:00+00:00"
        assert _run(monkeypatch, tmp_path, [_fx(1, "A", "B")], now) == 0
        capsys.readouterr()
        assert _run(monkeypatch, tmp_path, [_fx(1, "A", "B")], now) == 2
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "capture_exists" and out["captured"] is False
        assert "silently rewrite the evidence" in out["means"]


class TestItSaysWhatItWillContribute:
    def test_usable_fixtures_are_counted_with_their_lead(self, monkeypatch,
                                                          tmp_path, capsys):
        _run(monkeypatch, tmp_path,
             [_fx(1, "A", "B"), _fx(2, "C", "D")],
             "2026-08-06T22:30:00+00:00")
        out = json.loads(capsys.readouterr().out)
        assert out["n_usable"] == 2
        assert {u["lead_minutes"] for u in out["usable"]} == {60}

    def test_a_fixture_with_no_read_is_named_not_silently_dropped(
            self, monkeypatch, tmp_path, capsys):
        """Vancouver v Atlante had no read on matchday 1, and the
        operator staked $35 on it anyway. Naming it at capture time is
        how that becomes visible before settlement."""
        _run(monkeypatch, tmp_path,
             [_fx(1, "A", "B"), _fx(2, "Vancouver", "Atlante", read=None)],
             "2026-08-06T22:30:00+00:00")
        out = json.loads(capsys.readouterr().out)
        assert out["n_usable"] == 1
        assert out["no_strength_read"] == ["Vancouver v Atlante"]

    def test_a_capture_yielding_nothing_exits_1(self, monkeypatch, tmp_path):
        """A zero-yield capture must be loud AT CAPTURE TIME — at
        settlement it is too late to take another."""
        rc = _run(monkeypatch, tmp_path,
                  [_fx(1, "A", "B", status="1H", hg=1, ag=0)],
                  "2026-08-06T23:45:00+00:00")
        assert rc == 1

    def test_an_already_started_fixture_is_excluded_from_usable(
            self, monkeypatch, tmp_path, capsys):
        _run(monkeypatch, tmp_path,
             [_fx(1, "A", "B"),
              _fx(2, "C", "D", ko="2026-08-06T20:00:00+00:00")],
             "2026-08-06T22:30:00+00:00")
        out = json.loads(capsys.readouterr().out)
        assert out["n_usable"] == 1 and out["already_started_or_past"] == 1

    def test_no_api_base_refuses(self, monkeypatch, capsys):
        monkeypatch.delenv("TRIVELA_API_BASE", raising=False)
        monkeypatch.setattr(sys, "argv",
                            ["x", "--competition", "leagues-cup"])
        assert cap.main() == 2
        assert json.loads(capsys.readouterr().out)["captured"] is False

    def test_the_output_says_to_commit_it(self, monkeypatch, tmp_path,
                                           capsys):
        _run(monkeypatch, tmp_path, [_fx(1, "A", "B")],
             "2026-08-06T22:30:00+00:00")
        out = json.loads(capsys.readouterr().out)
        assert "dies with the container" in out["commit_it"]
